import "dotenv/config";
import { CosmWasmClient } from "@cosmjs/cosmwasm-stargate";

const { RPC, CONTRACT } = process.env;
if (!RPC || !CONTRACT) throw new Error("Missing env: RPC, CONTRACT");

async function main() {
  const client = await CosmWasmClient.connect(RPC);

  const ping = await client.queryContractSmart(CONTRACT, { ping: {} });
  console.log("PING:", ping);

  const val = await client.queryContractSmart(CONTRACT, { get_value: {} });
  console.log("GET_VALUE:", val);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});

