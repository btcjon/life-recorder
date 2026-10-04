export const TOKEN_STORAGE_KEY = "life-recorder-token";

export type StorageLike = {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem?(key: string): void;
};

export type AuthBootstrapInput = {
  hostname: string;
  hash: string;
  pathname: string;
  search: string;
  session: StorageLike;
  local?: StorageLike;
};

export type AuthBootstrap = {
  remote: boolean;
  token: string;
  url: string;
  clearedFragment: boolean;
};

export function isRemoteHostname(hostname: string): boolean {
  return hostname !== "127.0.0.1" && hostname !== "localhost";
}

export function bootstrapAuth(input: AuthBootstrapInput): AuthBootstrap {
  const remote = isRemoteHostname(input.hostname);
  const fragment = input.hash.replace(/^#/, "");
  let token = "";
  let clearedFragment = false;
  if (!remote) {
    token = fragment || input.session.getItem(TOKEN_STORAGE_KEY) || "";
    if (input.hash) {
      if (token) input.session.setItem(TOKEN_STORAGE_KEY, token);
      clearedFragment = true;
    }
  } else if (input.hash) {
    clearedFragment = true;
  }
  if (input.local?.getItem(TOKEN_STORAGE_KEY)) {
    input.local.removeItem?.(TOKEN_STORAGE_KEY);
  }
  const url = input.pathname + input.search;
  return { remote, token, url, clearedFragment };
}

export function readSessionToken(session: StorageLike): string {
  return session.getItem(TOKEN_STORAGE_KEY) || "";
}

export function clearSessionToken(session: StorageLike): void {
  session.removeItem?.(TOKEN_STORAGE_KEY);
  session.setItem(TOKEN_STORAGE_KEY, "");
}
