CREATE TABLE listings (
  id serial PRIMARY KEY,
  seller_id integer NOT NULL REFERENCES users(id),
  category text NOT NULL CHECK (category IN ('shoes','clothes','cars','electronics')),
  title text NOT NULL CHECK (length(title) BETWEEN 3 AND 120),
  description text NOT NULL CHECK (length(description) BETWEEN 1 AND 3000),
  price numeric(12,2) NOT NULL CHECK (price > 0),
  currency text NOT NULL CHECK (currency IN ('UGX','USD')),
  location text NOT NULL DEFAULT '' CHECK (length(location) <= 100),
  image text,
  status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','sold','archived')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX listings_browse ON listings(category, created_at DESC) WHERE status='active';
CREATE INDEX listings_seller ON listings(seller_id, created_at DESC);

CREATE TABLE conversations (
  id serial PRIMARY KEY,
  listing_id integer NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
  buyer_id integer NOT NULL REFERENCES users(id),
  seller_id integer NOT NULL REFERENCES users(id),
  last_message_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (listing_id, buyer_id),
  CHECK (buyer_id <> seller_id)
);
CREATE INDEX conversations_inbox ON conversations(buyer_id, seller_id, last_message_at DESC);

CREATE TABLE messages (
  id serial PRIMARY KEY,
  conversation_id integer NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  sender_id integer NOT NULL REFERENCES users(id),
  recipient_id integer NOT NULL REFERENCES users(id),
  body text NOT NULL CHECK (length(body) BETWEEN 1 AND 2000),
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (sender_id <> recipient_id)
);
CREATE INDEX messages_thread ON messages(conversation_id, created_at);
CREATE INDEX messages_unread ON messages(recipient_id, conversation_id) WHERE read_at IS NULL;
