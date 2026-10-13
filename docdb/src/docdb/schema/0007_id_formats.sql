CREATE TRIGGER document_id_format BEFORE INSERT ON document
WHEN NEW.doc_id NOT GLOB 'DOC-[A-Z0-9]*'
  OR NEW.doc_id GLOB '*[^A-Z0-9-]*'
  OR NEW.doc_id GLOB '*--*'
  OR NEW.doc_id GLOB '*-'
  OR length(NEW.doc_id) > 64
BEGIN SELECT RAISE(ABORT, 'doc_id format invalid'); END;

CREATE TRIGGER item_id_format BEFORE INSERT ON item
WHEN NEW.item_id NOT GLOB '[A-Z0-9]*-[A-Z0-9]*'
  OR NEW.item_id GLOB '*[^A-Z0-9-]*'
  OR NEW.item_id GLOB '*--*'
  OR NEW.item_id GLOB '*-'
  OR length(NEW.item_id) > 64
BEGIN SELECT RAISE(ABORT, 'item_id format invalid'); END;

CREATE TRIGGER work_id_format BEFORE INSERT ON work
WHEN NEW.work_id NOT GLOB 'WORK-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]*'
  OR substr(NEW.work_id, 11) GLOB '*[^0-9]*'
BEGIN SELECT RAISE(ABORT, 'work_id format invalid'); END;

CREATE TRIGGER run_id_format BEFORE INSERT ON run
WHEN NEW.run_id NOT GLOB 'RUN-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]*'
  OR substr(NEW.run_id, 10) GLOB '*[^0-9]*'
BEGIN SELECT RAISE(ABORT, 'run_id format invalid'); END;

CREATE TRIGGER handoff_id_format BEFORE INSERT ON handoff
WHEN NEW.handoff_id NOT GLOB 'HO-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]*'
  OR substr(NEW.handoff_id, 9) GLOB '*[^0-9]*'
BEGIN SELECT RAISE(ABORT, 'handoff_id format invalid'); END;

CREATE TRIGGER approval_id_format BEFORE INSERT ON approval
WHEN NEW.approval_id NOT GLOB 'APR-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]*'
  OR substr(NEW.approval_id, 10) GLOB '*[^0-9]*'
BEGIN SELECT RAISE(ABORT, 'approval_id format invalid'); END;

CREATE TRIGGER artifact_id_format BEFORE INSERT ON artifact
WHEN NEW.artifact_id NOT GLOB 'ART-[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9]*'
  OR substr(NEW.artifact_id, 10) GLOB '*[^0-9]*'
BEGIN SELECT RAISE(ABORT, 'artifact_id format invalid'); END;
