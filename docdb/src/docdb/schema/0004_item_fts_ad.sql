CREATE TRIGGER item_fts_ad AFTER DELETE ON item BEGIN
  INSERT INTO fts_text (fts_text, rowid, body)
  SELECT 'delete', unit_id, OLD.title || ' ' || OLD.attrs
  FROM text_unit WHERE item_id = OLD.item_id;
END;
