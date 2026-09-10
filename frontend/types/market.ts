export interface Tick {
  token:        string;
  exchange:     string;
  ltp:          number;
  open?:        number;
  high?:        number;
  low?:         number;
  close?:       number;
  volume?:      number;
  oi?:          number;
  best_bid?:    number;
  best_ask?:    number;
  best_bid_qty?: number;
  best_ask_qty?: number;
  ts?:          string;
}

export interface Candle {
  token:     string;
  symbol?:   string;
  interval:  string;
  timestamp: string;
  open:      number;
  high:      number;
  low:       number;
  close:     number;
  volume?:   number;
  oi?:       number;
}

export interface OptionStrike {
  token:       string;
  symbol:      string;
  strike:      number;
  option_type: "CE" | "PE";
  expiry:      string;
  ltp?:        number;
  iv?:         number;
  oi?:         number;
  volume?:     number;
  delta?:      number;
}

export interface OptionChain {
  underlying:     string;
  underlying_ltp: number;
  expiry:         string;
  strikes:        OptionStrike[];
  pcr?:           number;
  max_pain?:      number;
}

export interface Instrument {
  id:              number;
  token:           string;
  symbol:          string;
  name:            string;
  expiry?:         string;
  strike?:         number;
  option_type?:    string;
  instrument_type: string;
  lot_size:        number;
  tick_size:       number;
  exchange:        string;
  is_active:       boolean;
}

export interface SessionStatus {
  is_logged_in:    boolean;
  client_code?:    string;
  logged_in_at?:   string;
  feed_connected:  boolean;
  subscriptions:   number;
}

export interface FeedStatus {
  connected:     boolean;
  subscriptions: number;
}
