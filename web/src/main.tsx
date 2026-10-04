import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { bootstrapAuth, readSessionToken } from "./api/auth";
import { createClient } from "./api/client";
import "../vendor/shadcn/css/life-recorder-theme.css";

const session = window.sessionStorage;
const booted = bootstrapAuth({
  hostname: window.location.hostname,
  hash: window.location.hash,
  pathname: window.location.pathname,
  search: window.location.search,
  session,
  local: window.localStorage,
});

if (booted.clearedFragment) {
  history.replaceState(null, "", booted.url);
}

const client = createClient({
  getToken: () => readSessionToken(session),
  isRemote: () => booted.remote,
});

const root = document.getElementById("root");
if (!root) throw new Error("Missing root");

createRoot(root).render(
  <StrictMode>
    <App client={client} />
  </StrictMode>,
);
