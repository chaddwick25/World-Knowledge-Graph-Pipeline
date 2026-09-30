export interface ChannelMessage {
  id: string;
  type: "req" | "res";
  method?: string;
  args?: any[];
  result?: any;
  error?: string;
}
