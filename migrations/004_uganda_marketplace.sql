ALTER TABLE users DROP CONSTRAINT users_role_check;
UPDATE users u
SET role='seller'
WHERE u.role='customer'
  AND (
    EXISTS (SELECT 1 FROM listings l WHERE l.seller_id=u.id)
    OR EXISTS (SELECT 1 FROM subscriptions s WHERE s.user_id=u.id)
  );
ALTER TABLE users ADD CONSTRAINT users_role_check
  CHECK (role IN ('customer','seller','rider','admin'));

ALTER TABLE listings ADD COLUMN featured boolean NOT NULL DEFAULT false;

CREATE TABLE delivery_zones (
  id serial PRIMARY KEY,
  district text NOT NULL CHECK (length(district) BETWEEN 1 AND 80),
  town text NOT NULL CHECK (length(town) BETWEEN 1 AND 100),
  fee_ugx integer NOT NULL CHECK (fee_ugx >= 0),
  active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (district,town)
);

CREATE TABLE marketplace_commission_rates (
  id serial PRIMARY KEY,
  category text CHECK (category IN ('shoes','clothes','cars','electronics')),
  seller_id integer REFERENCES users(id),
  rate_percent numeric(5,2) NOT NULL CHECK (rate_percent BETWEEN 0 AND 100),
  active boolean NOT NULL DEFAULT true,
  CHECK (category IS NULL OR seller_id IS NULL)
);
CREATE UNIQUE INDEX marketplace_commission_default
  ON marketplace_commission_rates ((1)) WHERE category IS NULL AND seller_id IS NULL;
CREATE UNIQUE INDEX marketplace_commission_category
  ON marketplace_commission_rates (category) WHERE category IS NOT NULL;
CREATE UNIQUE INDEX marketplace_commission_seller
  ON marketplace_commission_rates (seller_id) WHERE seller_id IS NOT NULL;
INSERT INTO marketplace_commission_rates(rate_percent) VALUES (0);

CREATE TABLE marketplace_tax_rates (
  id serial PRIMARY KEY,
  category text CHECK (category IN ('shoes','clothes','cars','electronics')),
  rate_percent numeric(5,2) NOT NULL CHECK (rate_percent BETWEEN 0 AND 100),
  applies_to_delivery boolean NOT NULL DEFAULT false,
  active boolean NOT NULL DEFAULT true
);
CREATE UNIQUE INDEX marketplace_tax_default
  ON marketplace_tax_rates ((1)) WHERE category IS NULL;
CREATE UNIQUE INDEX marketplace_tax_category
  ON marketplace_tax_rates (category) WHERE category IS NOT NULL;
INSERT INTO marketplace_tax_rates(rate_percent,active) VALUES (0,false);

CREATE TABLE marketplace_settings (
  key text PRIMARY KEY,
  enabled boolean NOT NULL
);
INSERT INTO marketplace_settings(key,enabled) VALUES
  ('checkout_enabled',true),
  ('seller_subscriptions_enabled',true),
  ('featured_listings_enabled',false);

CREATE TABLE marketplace_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  reference text NOT NULL UNIQUE,
  buyer_id integer NOT NULL REFERENCES users(id),
  seller_id integer NOT NULL REFERENCES users(id),
  listing_id integer NOT NULL REFERENCES listings(id),
  listing_title text NOT NULL,
  currency text NOT NULL CHECK (currency='UGX'),
  product_subtotal_ugx integer NOT NULL CHECK (product_subtotal_ugx > 0),
  discount_ugx integer NOT NULL DEFAULT 0 CHECK (discount_ugx >= 0),
  delivery_fee_ugx integer NOT NULL DEFAULT 0 CHECK (delivery_fee_ugx >= 0),
  tax_rate_percent numeric(5,2) NOT NULL DEFAULT 0 CHECK (tax_rate_percent BETWEEN 0 AND 100),
  tax_applies_to_delivery boolean NOT NULL DEFAULT false,
  tax_ugx integer NOT NULL DEFAULT 0 CHECK (tax_ugx >= 0),
  total_ugx integer NOT NULL CHECK (total_ugx > 0),
  commission_rate_percent numeric(5,2) NOT NULL DEFAULT 0 CHECK (commission_rate_percent BETWEEN 0 AND 100),
  payment_fee_ugx integer NOT NULL DEFAULT 0 CHECK (payment_fee_ugx >= 0),
  efris_reference text NOT NULL DEFAULT '',
  efris_status text NOT NULL DEFAULT 'not_configured'
    CHECK (efris_status IN ('not_configured','pending','issued','failed')),
  delivery_method text NOT NULL CHECK (delivery_method IN ('seller','courier','pickup')),
  delivery_zone_id integer REFERENCES delivery_zones(id),
  recipient_name text NOT NULL,
  phone text NOT NULL,
  district text NOT NULL DEFAULT '',
  town text NOT NULL DEFAULT '',
  neighbourhood text NOT NULL DEFAULT '',
  landmark text NOT NULL DEFAULT '',
  map_pin text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'pending_payment'
    CHECK (status IN ('pending_payment','paid','confirmed','preparing','ready_for_pickup',
                      'out_for_delivery','delivered','cancelled','refunded','payment_review')),
  assigned_rider_id integer REFERENCES users(id),
  proof_of_delivery text NOT NULL DEFAULT '',
  paid_at timestamptz,
  delivered_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((delivery_method='pickup' AND delivery_zone_id IS NULL AND delivery_fee_ugx=0)
      OR (delivery_method<>'pickup' AND delivery_zone_id IS NOT NULL)),
  CHECK (discount_ugx<=product_subtotal_ugx),
  CHECK (total_ugx=product_subtotal_ugx-discount_ugx+delivery_fee_ugx+tax_ugx),
  CHECK (delivery_method='pickup' OR (district<>'' AND town<>'' AND neighbourhood<>'')),
  CHECK (buyer_id<>seller_id)
);
CREATE INDEX marketplace_orders_buyer ON marketplace_orders(buyer_id,created_at DESC);
CREATE INDEX marketplace_orders_seller ON marketplace_orders(seller_id,created_at DESC);
CREATE INDEX marketplace_orders_status ON marketplace_orders(status,created_at DESC);
CREATE UNIQUE INDEX marketplace_listing_one_open_order ON marketplace_orders(listing_id)
  WHERE status NOT IN ('cancelled','refunded');

CREATE TABLE marketplace_order_status_history (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES marketplace_orders(id) ON DELETE CASCADE,
  old_status text,
  new_status text NOT NULL,
  changed_by integer REFERENCES users(id),
  note text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX marketplace_order_history ON marketplace_order_status_history(order_id,created_at);

CREATE TABLE marketplace_order_notifications (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES marketplace_orders(id) ON DELETE CASCADE,
  user_id integer NOT NULL REFERENCES users(id),
  message text NOT NULL,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX marketplace_notifications_unread
  ON marketplace_order_notifications(user_id,created_at DESC) WHERE read_at IS NULL;

CREATE TABLE marketplace_payment_transactions (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES marketplace_orders(id),
  provider text NOT NULL,
  tracking_id text NOT NULL,
  transaction_reference text,
  status text NOT NULL DEFAULT 'pending',
  amount_ugx integer NOT NULL CHECK (amount_ugx > 0),
  currency text NOT NULL CHECK (currency='UGX'),
  fee_ugx integer NOT NULL DEFAULT 0 CHECK (fee_ugx >= 0),
  reconciliation_status text NOT NULL DEFAULT 'pending'
    CHECK (reconciliation_status IN ('pending','matched','review','refunded')),
  raw jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  verified_at timestamptz,
  UNIQUE(provider,tracking_id)
);
CREATE INDEX marketplace_payment_order ON marketplace_payment_transactions(order_id,created_at DESC);

CREATE TABLE marketplace_refunds (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES marketplace_orders(id),
  amount_ugx integer NOT NULL CHECK (amount_ugx > 0),
  provider text NOT NULL,
  transaction_reference text NOT NULL,
  note text NOT NULL DEFAULT '',
  created_by integer NOT NULL REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(provider,transaction_reference)
);

CREATE TABLE marketplace_ledger (
  id bigserial PRIMARY KEY,
  order_id uuid NOT NULL REFERENCES marketplace_orders(id),
  refund_id bigint REFERENCES marketplace_refunds(id),
  entry_type text NOT NULL CHECK (entry_type IN
    ('seller_proceeds','platform_commission','delivery_pass_through','payment_fee','tax','refund')),
  owner_id integer REFERENCES users(id),
  amount_ugx integer NOT NULL,
  payout_status text NOT NULL DEFAULT 'not_applicable'
    CHECK (payout_status IN ('not_applicable','pending','paid')),
  payout_reference text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX marketplace_ledger_order_entry
  ON marketplace_ledger(order_id,entry_type) WHERE refund_id IS NULL;
CREATE UNIQUE INDEX marketplace_ledger_refund_entry
  ON marketplace_ledger(refund_id,entry_type) WHERE refund_id IS NOT NULL;
CREATE INDEX marketplace_ledger_owner ON marketplace_ledger(owner_id,payout_status,created_at DESC);

CREATE TABLE marketplace_audit_events (
  id bigserial PRIMARY KEY,
  actor_id integer REFERENCES users(id),
  order_id uuid REFERENCES marketplace_orders(id),
  event_type text NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX marketplace_audit_order ON marketplace_audit_events(order_id,created_at DESC);
