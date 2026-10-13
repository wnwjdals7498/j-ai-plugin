ALTER TABLE cat_kind_field ADD COLUMN extract_rel TEXT
  CHECK (extract_rel IS NULL OR json_valid(extract_rel));
ALTER TABLE cat_item_type ADD COLUMN defined_by_field TEXT;
