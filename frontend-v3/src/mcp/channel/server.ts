import type { WebSocketServer } from "vite";
import crypto from "node:crypto";
import type { ChannelMessage } from "./types";

interface PendingRequest {
  resolve: (value: any) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export type CallClient<
  ClientFns extends Record<string, (...args: any[]) => any>,
> = <M extends keyof ClientFns & string>(
  method: M,
  ...args: Parameters<ClientFns[M]>
) => Promise<ReturnType<ClientFns[M]>>;

/**
 * Sets up a channel on Vite's WebSocket server to call client-side functions.
 *
 * Returns a typed `callClient` function to invoke any registered client function.
 *
 * @example
 * ```ts
 * configureServer(server) {
 *   const callClient = createChannel<ClientFns>(server.ws, 'my-plugin')
 *   const tree = await callClient('getInspectorTree')
 * }
 * ```
 */
export function createChannel<
  ClientFns extends Record<string, (...args: any[]) => any>,
>(ws: WebSocketServer, channel: string) {
  const EVENT = `${channel}:msg`;
  const TIMEOUT = 60_000;

  const pending = new Map<string, PendingRequest>();
  let lastKnownClient: any = null;

  // Register the client as soon as it announces itself (ping on connection)
  ws.on(`${channel}:ping`, (_data: unknown, client: any) => {
    lastKnownClient = client;
    console.log(`[channel] Client connected on "${channel}"`);
  });

  // Listen for responses from the client
  ws.on(EVENT, (data: ChannelMessage) => {
    if (data.type !== "res") return;

    const req = pending.get(data.id);
    if (req) {
      clearTimeout(req.timer);
      pending.delete(data.id);
      if (data.error) req.reject(new Error(data.error));
      else req.resolve(data.result);
    }
  });

  /**
   * Calls a client-side function and awaits its response.
   */
  function callClient<M extends keyof ClientFns & string>(
    method: M,
    ...args: Parameters<ClientFns[M]>
  ): Promise<ReturnType<ClientFns[M]>> {
    if (!lastKnownClient) {
      return Promise.reject(
        new Error(`[channel] callClient("${method}"): no connected client`),
      );
    }

    return new Promise((resolve, reject) => {
      const id = crypto.randomUUID();
      const timer = setTimeout(() => {
        pending.delete(id);
        reject(new Error(`[channel] timeout calling client "${method}"`));
      }, TIMEOUT);

      pending.set(id, { resolve, reject, timer });
      lastKnownClient.send(EVENT, {
        id,
        type: "req",
        method,
        args,
      } satisfies ChannelMessage);
    });
  }

  return callClient;
}
