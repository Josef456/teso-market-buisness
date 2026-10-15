ALTER TABLE marketplace_payment_transactions
  ADD COLUMN merchant_reference text;
UPDATE marketplace_payment_transactions p
SET merchant_reference=o.id::text
FROM marketplace_orders o
WHERE p.order_id=o.id AND p.merchant_reference IS NULL;
ALTER TABLE marketplace_payment_transactions
  ALTER COLUMN merchant_reference SET NOT NULL,
  ALTER COLUMN tracking_id DROP NOT NULL;
CREATE UNIQUE INDEX marketplace_payment_merchant_reference
  ON marketplace_payment_transactions(provider,merchant_reference);
