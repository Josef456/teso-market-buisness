CREATE TABLE marketplace_order_email_outbox (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES marketplace_orders(id) ON DELETE CASCADE,
  user_id integer NOT NULL REFERENCES users(id),
  event_key text NOT NULL,
  status text NOT NULL,
  message text NOT NULL,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  UNIQUE(order_id,user_id,event_key)
);
CREATE INDEX marketplace_order_email_outbox_pending
  ON marketplace_order_email_outbox(next_attempt_at,id) WHERE sent_at IS NULL;
