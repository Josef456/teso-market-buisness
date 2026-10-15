CREATE TABLE users (
  id serial PRIMARY KEY, email text UNIQUE NOT NULL, name text NOT NULL, phone text,
  password_hash text NOT NULL, role text NOT NULL DEFAULT 'customer' CHECK (role IN ('customer','admin')),
  disabled boolean NOT NULL DEFAULT false, reset_hash text, reset_expires timestamptz,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE plans (
  id serial PRIMARY KEY, name text NOT NULL, description text NOT NULL DEFAULT '',
  price_ugx integer NOT NULL CHECK (price_ugx > 0), price_usd integer CHECK (price_usd > 0),
  duration_days integer NOT NULL CHECK (duration_days > 0), active boolean NOT NULL DEFAULT true);
CREATE TABLE orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id integer NOT NULL REFERENCES users(id),
  plan_id integer NOT NULL REFERENCES plans(id), amount numeric(12,2) NOT NULL, currency text NOT NULL,
  status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','failed','cancelled','review')),
  tracking_id text, created_at timestamptz NOT NULL DEFAULT now(), paid_at timestamptz);
CREATE INDEX orders_tracking ON orders(tracking_id);
CREATE TABLE payments (
  id serial PRIMARY KEY, order_id uuid NOT NULL REFERENCES orders(id), tracking_id text NOT NULL,
  gateway text NOT NULL DEFAULT 'pesapal', status text, confirmation_code text, raw jsonb,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE subscriptions (
  id serial PRIMARY KEY, user_id integer NOT NULL REFERENCES users(id), plan_id integer NOT NULL REFERENCES plans(id),
  order_id uuid NOT NULL UNIQUE REFERENCES orders(id), starts_at timestamptz NOT NULL, ends_at timestamptz NOT NULL);
CREATE INDEX subs_user ON subscriptions(user_id, ends_at);
INSERT INTO plans(name,description,price_ugx,price_usd,duration_days) VALUES
 ('Starter','For individual sellers getting started. 1 shop, 50 listings.',20000,6,30),
 ('Business','For growing traders. 5 shops, unlimited listings, priority support.',60000,17,30),
 ('Annual','Business plan billed yearly. Two months free.',600000,165,365);
