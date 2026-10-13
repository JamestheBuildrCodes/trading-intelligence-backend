-- Create the per-trade journal table used by the API's list and upsert routes.
-- Direct browser roles stay blocked; the authenticated backend uses service_role.
CREATE TABLE IF NOT EXISTS public.trade_journal_entries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  account_id uuid NOT NULL REFERENCES public.trading_accounts(id) ON DELETE CASCADE,
  trade_id uuid NOT NULL REFERENCES public.trades(id) ON DELETE CASCADE,
  setup_name text NOT NULL DEFAULT '',
  thesis text NOT NULL DEFAULT '',
  pre_trade_emotion text NOT NULL DEFAULT '',
  post_trade_emotion text NOT NULL DEFAULT '',
  followed_plan boolean,
  planned_risk_amount numeric CHECK (planned_risk_amount IS NULL OR planned_risk_amount >= 0),
  planned_reward_r numeric CHECK (planned_reward_r IS NULL OR planned_reward_r >= 0),
  mistake_tags text[] NOT NULL DEFAULT '{}'::text[],
  review_notes text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT trade_journal_entries_account_trade_unique UNIQUE (account_id, trade_id)
);

CREATE INDEX IF NOT EXISTS trade_journal_entries_account_updated_idx
  ON public.trade_journal_entries (account_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS trade_journal_entries_trade_id_idx
  ON public.trade_journal_entries (trade_id);

ALTER TABLE public.trade_journal_entries ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.trade_journal_entries FROM anon, authenticated;
GRANT ALL ON TABLE public.trade_journal_entries TO service_role;
