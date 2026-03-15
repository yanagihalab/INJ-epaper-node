import "dotenv/config";

import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { PrivateKey } from "@injectivelabs/sdk-ts/core/accounts";
import { MsgBroadcasterWithPk } from "@injectivelabs/sdk-ts/core/tx";
import { MsgExecuteContract } from "@injectivelabs/sdk-ts/core/modules";

const { CONTRACT, MNEMONIC } = process.env;
if (!CONTRACT || !MNEMONIC) {
  throw new Error("Missing env: CONTRACT, MNEMONIC");
}

const BROADCAST_TIMEOUT_SEC = Number(process.env.NODE_BROADCAST_TIMEOUT_SEC ?? "180");

function nowNs() {
  return process.hrtime.bigint();
}

function nsToMs(ns) {
  return Number(ns) / 1e6;
}

function resolveNetwork(networkName) {
  switch ((networkName ?? "Testnet").trim()) {
    case "Mainnet":
      return Network.Mainnet;
    case "MainnetSentry":
      return Network.MainnetSentry;
    case "TestnetSentry":
      return Network.TestnetSentry ?? Network.Testnet;
    case "Testnet":
    default:
      return Network.Testnet;
  }
}

async function readStdin() {
  const chunks = [];
  for await (const c of process.stdin) chunks.push(c);
  const s = Buffer.concat(chunks).toString("utf-8").trim();
  if (!s) throw new Error("stdin is empty (expected JSON)");
  return JSON.parse(s);
}

function withTimeout(promise, timeoutMs, label = "operation") {
  let timer = null;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(() => {
      reject(new Error(`${label} timeout after ${timeoutMs} ms`));
    }, timeoutMs);
  });

  return Promise.race([promise, timeoutPromise]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

function pickTxHash(res) {
  return res?.txhash || res?.txHash || "";
}

async function main() {
  const input = await readStdin();

  const value = input.value;
  const memo = input.memo ?? "set_value from python";
  if (typeof value !== "string" || value.length === 0) {
    throw new Error("input.value must be non-empty string");
  }

  const network = resolveNetwork(process.env.INJ_NETWORK);
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

  const endpoints = getNetworkEndpoints(network);
  const broadcaster = new MsgBroadcasterWithPk({
    privateKey: pk,
    network,
    endpoints,
  });

  const t0 = nowNs();
  const res = await withTimeout(
    broadcaster.broadcast({
      injectiveAddress: address,
      msgs: msg,
      memo,
    }),
    Math.max(1, BROADCAST_TIMEOUT_SEC) * 1000,
    "broadcast"
  );
  const t1 = nowNs();

  const out = {
    ok: true,
    txhash: pickTxHash(res),
    broadcast_ms: Number(nsToMs(t1 - t0).toFixed(3)),
    height: res?.height ?? null,
    code: res?.code ?? null,
    gasWanted: res?.gasWanted ?? null,
    gasUsed: res?.gasUsed ?? null,
    timestamp: res?.timestamp ?? null,
    sender: address,
    contract: CONTRACT,
    network: String(network),
    memo,
    value_len: value.length,
  };

  process.stdout.write(JSON.stringify(out));
}

main().catch((e) => {
  const err = {
    ok: false,
    error: e?.message ?? String(e),
    error_type: e?.name ?? "Error",
    stack: typeof e?.stack === "string" ? e.stack.split("\n").slice(0, 6).join(" | ") : "",
  };
  process.stdout.write(JSON.stringify(err));
  process.exit(1);
});