-- Fail instead of truncating existing user data.
ALTER TABLE collections DROP CONSTRAINT collections_title_check;
ALTER TABLE collections DROP CONSTRAINT collections_description_check;
ALTER TABLE collections ADD CONSTRAINT collections_title_check CHECK (title IS NULL OR length(title)<=50);
ALTER TABLE collections ADD CONSTRAINT collections_description_check CHECK (description IS NULL OR length(description)<=200);
CREATE TABLE folder_roots (
    id uuid PRIMARY KEY,
    path text NOT NULL UNIQUE,
    approved_at timestamptz NOT NULL DEFAULT now()
);
