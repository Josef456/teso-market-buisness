ALTER TABLE plans
ADD COLUMN listing_limit integer CHECK (listing_limit IS NULL OR listing_limit > 0);

UPDATE plans
SET listing_limit = CASE
  WHEN description ILIKE '%50 listings%' THEN 50
  ELSE NULL
END;
