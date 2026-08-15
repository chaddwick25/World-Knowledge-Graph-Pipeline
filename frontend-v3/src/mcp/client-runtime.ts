import { connectChannel } from "./channel/client";
import { devtools } from "@vue/devtools-kit";
import type { ClientFunctions } from "./client-functions";
import { createClientFunctions } from "./client-functions";

devtools.init();

const clientFunctions: ClientFunctions = createClientFunctions(devtools);

connectChannel(import.meta.hot!, "vue-mcp", clientFunctions);
