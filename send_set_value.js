import "dotenv/config";

import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { PrivateKey } from "@injectivelabs/sdk-ts/core/accounts";
import { MsgBroadcasterWithPk } from "@injectivelabs/sdk-ts/core/tx";
import { MsgExecuteContract } from "@injectivelabs/sdk-ts/core/modules";

const { CONTRACT, MNEMONIC } = process.env;
if (!CONTRACT || !MNEMONIC) {
  throw new Error("Missing env: CONTRACT, MNEMONIC");
}

function nowNs() {
  return process.hrtime.bigint();
}
function nsToMs(ns) {
  return Number(ns) / 1e6;
}

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const s = Buffer.concat(chunks).toString("utf-8").trim();
  if (!s) throw new Error("stdin is empty (expected JSON)");
  return JSON.parse(s);
}

async function main() {
  // stdin で payload を受け取る（Pythonが決定した情報）
  // 例: { "value": "...", "memo": "...", "confirm": 0 }
  const input = await readStdin();

  // value は on-chain に保存する文字列。あなたの contract は set_value { value: String }。
  // 推奨：短め（unique_id）か、コンパクトなJSON文字列。
  const value = input.value;
  const memo = input.memo ?? "set_value from python";
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("input.value must be non-empty string");
  }

  const pk = PrivateKey.fromMnemonic(MNEMONIC);
  const address = pk.toAddress().toBech32();

  const msg = MsgExecuteContract.fromJSON({
    contractAddress: CONTRACT,
    sender: address,
    exec: {
      action: "set_value",
      msg: { value },
    },
  });

  const endpoints = getNetworkEndpoints(Network.Testnet);
  const broadcaster = new MsgBroadcasterWithPk({
    privateKey: pk,
    network: Network.Testnet,
    endpoints,
  });

  const t0 = nowNs();
  const res = await broadcaster.broadcast({
    injectiveAddress: address,
    msgs: msg,
    memo,
  });
  const t1 = nowNs();

  const txhash = res.txhash || res.txHash || "";
  const out = {
    ok: true,
    txhash,
    broadcast_ms: Number(nsToMs(t1 - t0).toFixed(3)),
    height: res.height ?? null,
    code: res.code ?? null,
    gasWanted: res.gasWanted ?? null,
    gasUsed: res.gasUsed ?? null,
    timestamp: res.timestamp ?? null,
    sender: address,
    contract: CONTRACT,
    value_len: value.length,
  };

  // stdout に JSON で返す（Pythonがパースする）
  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  const err = {
    ok: false,
    error: e?.message ?? String(e),
  };
  process.stdout.write(JSON.stringify(err));
  process.exit(1);
});
