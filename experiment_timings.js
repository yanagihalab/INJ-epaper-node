import "dotenv/config";
import fs from "node:fs";

import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { PrivateKey } from "@injectivelabs/sdk-ts/core/accounts";
import { MsgBroadcasterWithPk } from "@injectivelabs/sdk-ts/core/tx";
import { MsgExecuteContract } from "@injectivelabs/sdk-ts/core/modules";

const { CONTRACT, MNEMONIC } = process.env;

// 実験設定（環境変数で上書き可能）
const N = Number(process.env.N ?? "100");
const SLEEP_MS = Number(process.env.SLEEP_MS ?? "0");
const OUT = process.env.OUT ?? "timings.csv";

if (!CONTRACT || !MNEMONIC) {
  throw new Error("Missing env: CONTRACT, MNEMONIC");
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function nowNs() {
  return process.hrtime.bigint();
}
function nsToMs(ns) {
  return Number(ns) / 1e6;
}

// CSV escape
function esc(v) {
  const s = String(v ?? "");
  if (/[",\n]/.test(s)) return `"${s.replaceAll('"', '""')}"`;
  return s;
}

async function main() {
  const pk = PrivateKey.fromMnemonic(MNEMONIC);
  const address = pk.toAddress().toBech32();

  console.log("injectiveAddress:", address);
  console.log("contract:", CONTRACT);
  console.log(`N=${N} SLEEP_MS=${SLEEP_MS} OUT=${OUT}`);

  const endpoints = getNetworkEndpoints(Network.Testnet);
  const broadcaster = new MsgBroadcasterWithPk({
    privateKey: pk,
    network: Network.Testnet,
    endpoints,
  });

  const header = [
    "i",
    "value",
    "broadcast_ms",
    "txhash",
    "code",
    "height",
    "gasWanted",
    "gasUsed",
    "timestamp",
    "error",
  ];

  // 先にヘッダを書いておく（途中で落ちても残る）
  fs.writeFileSync(OUT, header.join(",") + "\n", "utf8");

  for (let i = 1; i <= N; i++) {
    const value = `pi-exp-${Date.now()}-${i}`;

    const msg = MsgExecuteContract.fromJSON({
      contractAddress: CONTRACT,
      sender: address,
      exec: {
        action: "set_value",
        msg: { value },
      },
    });

    const t0 = nowNs();

    let res = null;
    let errMsg = "";

    // 軽いリトライ（sequence系など一時失敗の吸収）
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        res = await broadcaster.broadcast({
          injectiveAddress: address,
          msgs: msg,
          memo: `pi-exp #${i}`,
        });
        errMsg = "";
        break;
      } catch (e) {
        errMsg = (e && (e.message || String(e))) || "unknown error";
        if (/sequence|account sequence|incorrect/i.test(errMsg)) {
          await sleep(1200 * attempt);
        } else {
          await sleep(400 * attempt);
        }
      }
    }

    const t1 = nowNs();
    const broadcastMs = nsToMs(t1 - t0);

    // res の形は環境で揺れるので吸収
    const txhash = res?.txhash || res?.txHash || "";
    const code = res?.code ?? "";
    const height = res?.height ?? "";
    const gasWanted = res?.gasWanted ?? "";
    const gasUsed = res?.gasUsed ?? "";
    const timestamp = res?.timestamp ?? "";

    const row = {
      i,
      value,
      broadcast_ms: broadcastMs.toFixed(3),
      txhash,
      code,
      height,
      gasWanted,
      gasUsed,
      timestamp,
      error: res ? "" : errMsg,
    };

    const line = header.map((k) => esc(row[k])).join(",") + "\n";
    fs.appendFileSync(OUT, line, "utf8");

    if (res) {
      console.log(`[${i}/${N}] ok broadcast_ms=${broadcastMs.toFixed(1)} txhash=${txhash}`);
    } else {
      console.log(`[${i}/${N}] ERROR broadcast_ms=${broadcastMs.toFixed(1)} msg=${errMsg}`);
    }

    if (SLEEP_MS > 0) await sleep(SLEEP_MS);
  }

  console.log(`wrote: ${OUT}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
