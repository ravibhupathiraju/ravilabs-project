//+------------------------------------------------------------------+
//|                                            SwingStrategies.mq5   |
//|  Port of the Swing Trade Tracker strategies to MetaTrader 5.     |
//|                                                                  |
//|  Seven daily-bar strategies behind one input switch, so the      |
//|  Strategy Tester can optimize over InpStrategy (0..6) and give   |
//|  one comparable result row per strategy - the MT5 equivalent of  |
//|  the app's backtest results table.                               |
//|                                                                  |
//|  Execution model (mirrors backtest.py):                          |
//|    - signals are read off the last CLOSED daily bar              |
//|    - the position is opened at the next bar's open               |
//|    - the ATR stop / ATR target are fixed at the fill and sent as |
//|      SL/TP, so they fill intrabar at their price                 |
//|    - signal exits and the time stop close at market              |
//|                                                                  |
//|  Indicators are computed from raw OHLCV here (not via iRSI/iMA)  |
//|  so they match the pandas definitions in indicators.py exactly:  |
//|  Wilder RSI/ATR, EMA-9 MACD signal (MT5's built-in MACD uses an  |
//|  SMA signal line), and a sample-stdev Bollinger band.            |
//+------------------------------------------------------------------+
#property copyright "Swing Trade Tracker"
#property version   "1.00"

#include <Trade\Trade.mqh>

//--- strategy switch: optimize this input 0..6 to test all seven
enum ENUM_SWING_STRATEGY
  {
   STRAT_RSI2       = 0,  // RSI(2) Mean Reversion
   STRAT_DOUBLE7    = 1,  // Double 7s
   STRAT_BOLLINGER  = 2,  // Bollinger Snapback
   STRAT_PULLBACK20 = 3,  // SMA20 Trend Pullback
   STRAT_BREAKOUT20 = 4,  // 20-Day Breakout
   STRAT_MACD       = 5,  // MACD Trend Cross
   STRAT_BEAR_CRWV  = 6   // Bear CRWV Pullback Score
  };

input group                "=== Strategy ==="
input ENUM_SWING_STRATEGY  InpStrategy      = STRAT_RSI2;  // Strategy (optimize 0..6)
input int                  InpBearMinScore  = 45;          // Bear CRWV: min score (0-80)

input group                "=== Universe ==="
input string               InpSymbolList    = "";          // Symbols, comma separated (empty = chart symbol)

input group                "=== Risk ==="
input double               InpRiskPercent   = 1.0;         // Risk per trade (% of equity); 0 = use fixed lots
input double               InpFixedLots     = 1.0;         // Fixed lots when risk % = 0

input group                "=== Filters (set to 0 to disable) ==="
input double               InpMinPrice      = 5.0;         // Min price
input double               InpMinAvgVolume  = 500000;      // Min 20-day avg volume (0 if broker has no real volume)

input group                "=== Engine ==="
input int                  InpHistoryBars   = 400;         // Warmup bars loaded per signal check
input ulong                InpMagic         = 20260713;    // Magic number
input ulong                InpSlippage      = 20;          // Max deviation (points)

//--- per-symbol state
struct SymbolCtx
  {
   string            sym;
   datetime          lastBar;
  };

SymbolCtx  g_ctx[];
CTrade     g_trade;
bool       g_volume_warned = false;

//--- strategy risk parameters, indexed by ENUM_SWING_STRATEGY
//    (stop ATR mult, target ATR mult [0 = none], max hold days)
double  g_stop_mult[7]   = {2.0, 2.0, 2.0, 1.5, 2.5, 2.5, 1.5};
double  g_target_mult[7] = {0.0, 0.0, 0.0, 3.0, 0.0, 0.0, 3.0};
int     g_max_hold[7]    = { 10,  15,  10,  15,  20,  30,  10};

//+------------------------------------------------------------------+
//| Bar data + indicators for one symbol, series-indexed              |
//| (index 0 = forming bar, index 1 = last closed bar)                |
//+------------------------------------------------------------------+
struct Bars
  {
   int      n;
   double   high[], low[], close[];
   double   vol[];
   double   sma5[], sma20[], sma50[], sma200[];
   double   ema5[], ema21[], ema50[];
   double   rsi2[], rsi14[], atr14[];
   double   macd[], macd_sig[];
   double   bb_lower[], std20[];
   double   avg_vol20[];
  };

//+------------------------------------------------------------------+
//| Helpers: rolling window stats over a series-indexed array         |
//| Window [from .. from+len-1] going back in time.                   |
//+------------------------------------------------------------------+
double WinMax(const double &a[], int from, int len)
  {
   double m = a[from];
   for(int i = from + 1; i < from + len; i++)
      if(a[i] > m) m = a[i];
   return m;
  }

double WinMin(const double &a[], int from, int len)
  {
   double m = a[from];
   for(int i = from + 1; i < from + len; i++)
      if(a[i] < m) m = a[i];
   return m;
  }

double WinMean(const double &a[], int from, int len)
  {
   double s = 0.0;
   for(int i = from; i < from + len; i++) s += a[i];
   return s / len;
  }

//--- index (offset from `from`) of the highest value in the window
int WinArgMax(const double &a[], int from, int len)
  {
   int best = 0;
   for(int i = 1; i < len; i++)
      if(a[from + i] > a[from + best]) best = i;
   return best;
  }

//+------------------------------------------------------------------+
//| Load bars and compute every indicator the strategies need         |
//+------------------------------------------------------------------+
bool LoadBars(const string sym, Bars &b)
  {
   int need = InpHistoryBars;
   ArraySetAsSeries(b.high,  true);
   ArraySetAsSeries(b.low,   true);
   ArraySetAsSeries(b.close, true);

   if(CopyHigh (sym, PERIOD_D1, 0, need, b.high)  < need) return false;
   if(CopyLow  (sym, PERIOD_D1, 0, need, b.low)   < need) return false;
   if(CopyClose(sym, PERIOD_D1, 0, need, b.close) < need) return false;

   int n = ArraySize(b.close);
   b.n = n;

   //--- volume: prefer real (exchange) volume; fall back to tick volume
   long rv[];
   ArraySetAsSeries(rv, true);
   ArrayResize(b.vol, n);
   ArraySetAsSeries(b.vol, true);
   bool have_real = (CopyRealVolume(sym, PERIOD_D1, 0, need, rv) == need);
   double rsum = 0.0;
   if(have_real)
      for(int i = 0; i < n; i++) rsum += (double)rv[i];
   if(have_real && rsum > 0.0)
     {
      for(int i = 0; i < n; i++) b.vol[i] = (double)rv[i];
     }
   else
     {
      long tv[];
      ArraySetAsSeries(tv, true);
      if(CopyTickVolume(sym, PERIOD_D1, 0, need, tv) < need) return false;
      for(int i = 0; i < n; i++) b.vol[i] = (double)tv[i];
      if(!g_volume_warned)
        {
         PrintFormat("WARNING: %s has no real volume; using tick volume. "
                     "Set InpMinAvgVolume=0 and note the 20-Day Breakout "
                     "volume filter is only a proxy.", sym);
         g_volume_warned = true;
        }
     }

   ArrayResize(b.sma5, n);   ArraySetAsSeries(b.sma5, true);
   ArrayResize(b.sma20, n);  ArraySetAsSeries(b.sma20, true);
   ArrayResize(b.sma50, n);  ArraySetAsSeries(b.sma50, true);
   ArrayResize(b.sma200, n); ArraySetAsSeries(b.sma200, true);
   ArrayResize(b.ema5, n);   ArraySetAsSeries(b.ema5, true);
   ArrayResize(b.ema21, n);  ArraySetAsSeries(b.ema21, true);
   ArrayResize(b.ema50, n);  ArraySetAsSeries(b.ema50, true);
   ArrayResize(b.rsi2, n);   ArraySetAsSeries(b.rsi2, true);
   ArrayResize(b.rsi14, n);  ArraySetAsSeries(b.rsi14, true);
   ArrayResize(b.atr14, n);  ArraySetAsSeries(b.atr14, true);
   ArrayResize(b.macd, n);   ArraySetAsSeries(b.macd, true);
   ArrayResize(b.macd_sig, n); ArraySetAsSeries(b.macd_sig, true);
   ArrayResize(b.bb_lower, n); ArraySetAsSeries(b.bb_lower, true);
   ArrayResize(b.std20, n);  ArraySetAsSeries(b.std20, true);
   ArrayResize(b.avg_vol20, n); ArraySetAsSeries(b.avg_vol20, true);

   //--- EMAs (adjust=False recursion, seeded at the oldest bar)
   double ema12[], ema26[];
   ArrayResize(ema12, n); ArraySetAsSeries(ema12, true);
   ArrayResize(ema26, n); ArraySetAsSeries(ema26, true);
   EmaSeries(b.close, b.ema5,  n, 5);
   EmaSeries(b.close, b.ema21, n, 21);
   EmaSeries(b.close, b.ema50, n, 50);
   EmaSeries(b.close, ema12,   n, 12);
   EmaSeries(b.close, ema26,   n, 26);

   //--- MACD line, then EMA-9 signal (pandas uses an EMA signal, MT5's
   //    built-in iMACD uses an SMA - hence the manual computation)
   for(int i = 0; i < n; i++) b.macd[i] = ema12[i] - ema26[i];
   EmaSeries(b.macd, b.macd_sig, n, 9);

   //--- Wilder RSI(2) and RSI(14)
   RsiSeries(b.close, b.rsi2,  n, 2);
   RsiSeries(b.close, b.rsi14, n, 14);

   //--- Wilder ATR(14)
   AtrSeries(b.high, b.low, b.close, b.atr14, n, 14);

   //--- SMAs, Bollinger lower band (sample stdev, like pandas .std())
   for(int i = 0; i < n; i++)
     {
      b.sma5[i]   = (i + 5   <= n) ? WinMean(b.close, i, 5)   : EMPTY_VALUE;
      b.sma20[i]  = (i + 20  <= n) ? WinMean(b.close, i, 20)  : EMPTY_VALUE;
      b.sma50[i]  = (i + 50  <= n) ? WinMean(b.close, i, 50)  : EMPTY_VALUE;
      b.sma200[i] = (i + 200 <= n) ? WinMean(b.close, i, 200) : EMPTY_VALUE;
      b.avg_vol20[i] = (i + 20 <= n) ? WinMean(b.vol, i, 20)  : EMPTY_VALUE;

      if(i + 20 <= n)
        {
         double m = b.sma20[i], ss = 0.0;
         for(int k = i; k < i + 20; k++) ss += (b.close[k] - m) * (b.close[k] - m);
         b.std20[i]    = MathSqrt(ss / 19.0);          // ddof=1, as pandas
         b.bb_lower[i] = m - 2.0 * b.std20[i];
        }
      else
        {
         b.std20[i] = EMPTY_VALUE;
         b.bb_lower[i] = EMPTY_VALUE;
        }
     }
   return true;
  }

//--- EMA with adjust=False semantics, seeded at the oldest bar
void EmaSeries(const double &src[], double &dst[], int n, int span)
  {
   double a = 2.0 / (span + 1.0);
   dst[n - 1] = src[n - 1];
   for(int i = n - 2; i >= 0; i--)
      dst[i] = a * src[i] + (1.0 - a) * dst[i + 1];
  }

//--- Wilder RSI: EMA of gains/losses with alpha = 1/period
void RsiSeries(const double &close[], double &dst[], int n, int period)
  {
   double a = 1.0 / period;
   double avg_gain = 0.0, avg_loss = 0.0;
   dst[n - 1] = EMPTY_VALUE;
   for(int i = n - 2; i >= 0; i--)
     {
      double delta = close[i] - close[i + 1];
      double gain  = (delta > 0.0) ?  delta : 0.0;
      double loss  = (delta < 0.0) ? -delta : 0.0;
      if(i == n - 2) { avg_gain = gain; avg_loss = loss; }
      else
        {
         avg_gain = a * gain + (1.0 - a) * avg_gain;
         avg_loss = a * loss + (1.0 - a) * avg_loss;
        }
      dst[i] = (avg_loss <= 0.0) ? 100.0
                                 : 100.0 - 100.0 / (1.0 + avg_gain / avg_loss);
     }
  }

//--- Wilder ATR: EMA of True Range with alpha = 1/period
void AtrSeries(const double &high[], const double &low[], const double &close[],
               double &dst[], int n, int period)
  {
   double a = 1.0 / period;
   double atr = 0.0;
   dst[n - 1] = EMPTY_VALUE;
   for(int i = n - 2; i >= 0; i--)
     {
      double pc = close[i + 1];
      double tr = MathMax(high[i] - low[i],
                  MathMax(MathAbs(high[i] - pc), MathAbs(low[i] - pc)));
      atr = (i == n - 2) ? tr : a * tr + (1.0 - a) * atr;
      dst[i] = atr;
     }
  }

//+------------------------------------------------------------------+
//| Bear CRWV score (0-80) on bar `i` - port of _bear_columns()       |
//| The SPY market-regime bonus is not included, matching the         |
//| BearCRWV strategy class used by the app's backtest and alerts.    |
//+------------------------------------------------------------------+
int BearScore(const Bars &b, int i)
  {
   if(i + 60 > b.n || b.avg_vol20[i] == EMPTY_VALUE) return -1;

   bool trend_up = (b.ema5[i] > b.ema21[i]) && (b.ema21[i] > b.ema50[i]);
   bool ema21_rising = (i + 10 < b.n) && (b.ema21[i] > b.ema21[i + 10]);

   double hi60 = WinMax(b.high, i, 60);
   double pullback_pct = (hi60 - b.close[i]) / hi60 * 100.0;
   int bars_since_high = WinArgMax(b.high, i, 60);   // 0 = high is this bar

   double rel_vol = (b.avg_vol20[i] > 0.0) ? b.vol[i] / b.avg_vol20[i] : 0.0;

   int score = 0;
   if(trend_up)      score += 20;   // TREND_UP + EMA_ALIGNMENT (same test upstream)
   if(ema21_rising)  score += 5;
   if(pullback_pct >= 10.0 && pullback_pct <= 30.0) score += 15;
   if(bars_since_high >= 5 && bars_since_high <= 25) score += 10;
   if(b.rsi14[i] < 45.0) score += 10;
   if(rel_vol > 1.20)    score += 10;
   return score;
  }

//+------------------------------------------------------------------+
//| Liquidity filter shared by every strategy                         |
//+------------------------------------------------------------------+
bool Liquid(const Bars &b, int i)
  {
   if(b.avg_vol20[i] == EMPTY_VALUE || b.atr14[i] == EMPTY_VALUE) return false;
   if(InpMinPrice > 0.0 && b.close[i] <= InpMinPrice) return false;
   if(InpMinAvgVolume > 0.0 && b.avg_vol20[i] <= InpMinAvgVolume) return false;
   return true;
  }

//+------------------------------------------------------------------+
//| Entry rules - one per strategy, read off the last closed bar      |
//+------------------------------------------------------------------+
bool EntrySignal(const Bars &b, int i)
  {
   if(!Liquid(b, i)) return false;

   switch(InpStrategy)
     {
      case STRAT_RSI2:
        {
         if(b.sma200[i] == EMPTY_VALUE || b.sma5[i] == EMPTY_VALUE) return false;
         return b.close[i] > b.sma200[i] && b.rsi2[i] < 10.0 && b.close[i] < b.sma5[i];
        }

      case STRAT_DOUBLE7:
        {
         if(b.sma200[i] == EMPTY_VALUE || i + 7 > b.n) return false;
         double lo7 = WinMin(b.close, i, 7);
         return b.close[i] > b.sma200[i] && b.close[i] <= lo7;
        }

      case STRAT_BOLLINGER:
        {
         if(b.sma200[i] == EMPTY_VALUE || b.bb_lower[i] == EMPTY_VALUE) return false;
         return b.close[i] > b.sma200[i] && b.close[i] < b.bb_lower[i];
        }

      case STRAT_PULLBACK20:
        {
         if(b.sma20[i] == EMPTY_VALUE || b.sma50[i] == EMPTY_VALUE ||
            b.sma200[i] == EMPTY_VALUE) return false;
         return b.sma50[i] > b.sma200[i]
             && b.close[i] > b.sma50[i]
             && b.low[i]  <= b.sma20[i]
             && b.close[i] > b.sma20[i];
        }

      case STRAT_BREAKOUT20:
        {
         if(b.sma200[i] == EMPTY_VALUE || i + 21 > b.n) return false;
         double hi20_prior = WinMax(b.high, i + 1, 20);   // .shift(1)
         return b.close[i] > hi20_prior
             && b.close[i] > b.sma200[i]
             && b.vol[i]   > 1.5 * b.avg_vol20[i];
        }

      case STRAT_MACD:
        {
         if(b.sma200[i] == EMPTY_VALUE || i + 1 >= b.n) return false;
         bool cross_up = (b.macd[i] > b.macd_sig[i]) &&
                         (b.macd[i + 1] <= b.macd_sig[i + 1]);
         return cross_up && b.close[i] > b.sma200[i];
        }

      case STRAT_BEAR_CRWV:
        {
         int score = BearScore(b, i);
         return score >= InpBearMinScore;
        }
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Signal exit rules (stop / target are SL/TP; time stop is separate)|
//+------------------------------------------------------------------+
bool SignalExit(const Bars &b, int i)
  {
   switch(InpStrategy)
     {
      case STRAT_RSI2:
         if(b.sma5[i] == EMPTY_VALUE) return false;
         return b.close[i] > b.sma5[i] || b.rsi2[i] > 70.0;

      case STRAT_DOUBLE7:
        {
         if(i + 7 > b.n) return false;
         double hi7 = WinMax(b.close, i, 7);
         return b.close[i] >= hi7;
        }

      case STRAT_BOLLINGER:
         if(b.sma20[i] == EMPTY_VALUE) return false;
         return b.close[i] > b.sma20[i];

      case STRAT_PULLBACK20:
         if(b.sma50[i] == EMPTY_VALUE) return false;
         return b.close[i] < b.sma50[i];

      case STRAT_BREAKOUT20:
        {
         if(i + 11 > b.n) return false;
         double lo10_prior = WinMin(b.low, i + 1, 10);    // .shift(1)
         return b.close[i] < lo10_prior;
        }

      case STRAT_MACD:
        {
         if(i + 1 >= b.n) return false;
         return (b.macd[i] < b.macd_sig[i]) &&
                (b.macd[i + 1] >= b.macd_sig[i + 1]);
        }

      case STRAT_BEAR_CRWV:
         return false;   // exits on stop, target or the 10-day time stop only
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Position sizing from the stop distance                            |
//+------------------------------------------------------------------+
double LotsFor(const string sym, double entry, double stop)
  {
   if(InpRiskPercent <= 0.0) return NormalizeLots(sym, InpFixedLots);

   double risk_money = AccountInfoDouble(ACCOUNT_EQUITY) * InpRiskPercent / 100.0;
   double dist = MathAbs(entry - stop);
   if(dist <= 0.0) return 0.0;

   double tick_size  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   if(tick_size <= 0.0 || tick_value <= 0.0) return NormalizeLots(sym, InpFixedLots);

   double loss_per_lot = dist / tick_size * tick_value;
   if(loss_per_lot <= 0.0) return 0.0;
   return NormalizeLots(sym, risk_money / loss_per_lot);
  }

double NormalizeLots(const string sym, double lots)
  {
   double min  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double max  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   double step = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   if(step <= 0.0) step = 0.01;
   lots = MathFloor(lots / step) * step;
   if(lots < min) lots = 0.0;      // cannot risk this little: skip the trade
   if(lots > max) lots = max;
   return lots;
  }

//+------------------------------------------------------------------+
//| Find this EA's open position on a symbol                          |
//+------------------------------------------------------------------+
bool HasPosition(const string sym, datetime &open_time)
  {
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != sym) continue;
      if(PositionGetInteger(POSITION_MAGIC) != (long)InpMagic) continue;
      open_time = (datetime)PositionGetInteger(POSITION_TIME);
      return true;
     }
   return false;
  }

//+------------------------------------------------------------------+
//| Per-symbol logic, run once at the open of each new daily bar      |
//+------------------------------------------------------------------+
void OnNewDailyBar(const string sym)
  {
   Bars b;
   if(!LoadBars(sym, b)) return;

   const int i = 1;                   // the bar that just closed = signal bar
   int s = (int)InpStrategy;

   datetime open_time;
   if(HasPosition(sym, open_time))
     {
      //--- stop and target are live as SL/TP; check signal exit + time stop.
      //    days_held mirrors backtest.py: 0 on the entry bar itself.
      int entry_shift = iBarShift(sym, PERIOD_D1, open_time, false);
      int days_held   = entry_shift - 1;
      if(days_held < 0) days_held = 0;

      if(SignalExit(b, i))
        {
         g_trade.PositionClose(sym, InpSlippage);
         return;
        }
      if(days_held >= g_max_hold[s])
        {
         g_trade.PositionClose(sym, InpSlippage);
         return;
        }
      return;                          // one position at a time per symbol
     }

   //--- flat: look for an entry on the closed bar, fill at this bar's open
   if(!EntrySignal(b, i)) return;

   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   if(ask <= 0.0) return;
   double atr = b.atr14[i];
   if(atr <= 0.0) return;

   double stop   = ask - g_stop_mult[s] * atr;
   double target = (g_target_mult[s] > 0.0) ? ask + g_target_mult[s] * atr : 0.0;

   double lots = LotsFor(sym, ask, stop);
   if(lots <= 0.0) return;

   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   stop = NormalizeDouble(stop, digits);
   if(target > 0.0) target = NormalizeDouble(target, digits);

   g_trade.Buy(lots, sym, 0.0, stop, target, EnumToString(InpStrategy));
  }

//+------------------------------------------------------------------+
//| Init / deinit / tick                                              |
//+------------------------------------------------------------------+
int OnInit()
  {
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippage);
   g_trade.SetTypeFillingBySymbol(_Symbol);
   g_trade.LogLevel(LOG_LEVEL_ERRORS);

   //--- build the symbol list
   string list = InpSymbolList;
   StringTrimLeft(list);
   StringTrimRight(list);
   if(list == "")
     {
      ArrayResize(g_ctx, 1);
      g_ctx[0].sym = _Symbol;
      g_ctx[0].lastBar = 0;
     }
   else
     {
      string parts[];
      int cnt = StringSplit(list, ',', parts);
      ArrayResize(g_ctx, 0);
      for(int i = 0; i < cnt; i++)
        {
         string s = parts[i];
         StringTrimLeft(s);
         StringTrimRight(s);
         if(s == "") continue;
         if(!SymbolSelect(s, true))
           {
            PrintFormat("WARNING: symbol %s not found at this broker; skipped", s);
            continue;
           }
         int k = ArraySize(g_ctx);
         ArrayResize(g_ctx, k + 1);
         g_ctx[k].sym = s;
         g_ctx[k].lastBar = 0;
        }
     }

   if(ArraySize(g_ctx) == 0)
     {
      Print("ERROR: no tradable symbols resolved from InpSymbolList");
      return INIT_PARAMETERS_INCORRECT;
     }

   PrintFormat("SwingStrategies: %s on %d symbol(s), stop %.1fxATR, target %.1fxATR, %d-day hold",
               EnumToString(InpStrategy), ArraySize(g_ctx),
               g_stop_mult[(int)InpStrategy], g_target_mult[(int)InpStrategy],
               g_max_hold[(int)InpStrategy]);
   return INIT_SUCCEEDED;
  }

void OnTick()
  {
   for(int i = 0; i < ArraySize(g_ctx); i++)
     {
      datetime bt[];
      if(CopyTime(g_ctx[i].sym, PERIOD_D1, 0, 1, bt) != 1) continue;
      if(bt[0] == g_ctx[i].lastBar) continue;      // same bar: nothing to do

      g_ctx[i].lastBar = bt[0];
      OnNewDailyBar(g_ctx[i].sym);
     }
  }
//+------------------------------------------------------------------+
