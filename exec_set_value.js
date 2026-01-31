import "dotenv/config";
import { CosmWasmClient } from "@cosmjs/cosmwasm-stargate";

import { Network, getNetworkEndpoints } from "@injectivelabs/networks";
import { PrivateKey } from "@injectivelabs/sdk-ts/core/accounts";
import { MsgBroadcasterWithPk } from "@injectivelabs/sdk-ts/core/tx";
import { MsgExecuteContract } from "@injectivelabs/sdk-ts/core/modules";

const { RPC, CONTRACT, MNEMONIC } = process.env;
if (!RPC || !CONTRACT || !MNEMONIC) {
  throw new Error("Missing env: RPC, CONTRACT, MNEMONIC");
}

// 毎回値が変わるように
const newValue = `hello-from-pi-${Date.now()}`;

async function queryValue() {
  const client = await CosmWasmClient.connect(RPC);
  return client.queryContractSmart(CONTRACT, { get_value: {} });
}

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function main() {
  // Injective鍵導出
  const pk = PrivateKey.fromMnemonic(MNEMONIC);
  const address = pk.toAddress().toBech32();

  console.log("injectiveAddress:", address);
  console.log("contract:", CONTRACT);
  console.log("set_value:", newValue);

  // execute msg（このコントラクトは {"set_value":{"value":...}} を受ける）
  const msg = MsgExecuteContract.fromJSON({
    contractAddress: CONTRACT,
    sender: address,
    exec: {
      action: "set_value",
      msg: { value: newValue },
    },
  });

  // broadcaster（PrivateKey インスタンスをそのまま渡す）
  const endpoints = getNetworkEndpoints(Network.Testnet);
  const broadcaster = new MsgBroadcasterWithPk({
    privateKey: pk,
    network: Network.Testnet,
    endpoints,
  });

  const txHash = await broadcaster.broadcast({
    injectiveAddress: address,
    msgs: msg,
    memo: "set_value from raspberry pi",
  });

  console.log("txHash:", txHash);

  // 反映確認（最大90秒待つ）
  for (let i = 0; i < 45; i++) {
    const v = await queryValue();
    if (v?.value === newValue) {
      console.log("CONFIRMED:", v);
      return;
    }
    console.log("waiting... current:", v);
    await sleep(2000);
  }

  console.log("Not confirmed yet. Run: node query.js");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
