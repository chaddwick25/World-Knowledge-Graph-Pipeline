import type { ChannelMessage } from "./types";

/**
 * Connects to a channel via `import.meta.hot` and registers
 * client functions callable by the server.
 *
 * @example
 * ```ts
 * connectChannel(import.meta.hot!, 'my-plugin', {
 *   getInspectorTree: () => fetchTree(),
 * })
 * ```
 */
export function connectChannel<
  ClientFns extends Record<string, (...args: any[]) => any>,
>(
  hot: NonNullable<ImportMeta["hot"]>,
  channel: string,
  clientFunctions: ClientFns,
) {
  const EVENT = `${channel}:msg`;

  // Announce to the server so it knows about this client
  hot.send(`${channel}:ping`, {});

  // Listen for requests from the server and respond
  hot.on(EVENT, async (data: ChannelMessage) => {
    if (data.type !== "req") return;

    const fn = clientFunctions[data.method!];
    if (!fn) {
      hot.send(EVENT, {
        id: data.id,
        type: "res",
        error: `[channel] unknown client function "${data.method}"`,
      } satisfies ChannelMessage);
      return;
    }

    try {
      const result = await fn(...(data.args || []));
      hot.send(EVENT, {
        id: data.id,
        type: "res",
        result,
      } satisfies ChannelMessage);
    } catch (error_: any) {
      hot.send(EVENT, {
        id: data.id,
        type: "res",
        error: error_?.message ?? String(error_),
      } satisfies ChannelMessage);
    }
  });
}
