-- pgcrypto provides gen_random_uuid(), used for primary keys, and digest()
-- for hashing entity identifiers inside the database when needed.
--
-- UUIDs over sequential integers: decision IDs are returned to merchants and
-- appear in support tickets. A sequential ID would leak total transaction
-- volume to anyone who sees two of them.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- btree_gist enables exclusion constraints mixing equality and range tests,
-- used later to guarantee only one ruleset version can be active at a time.
CREATE EXTENSION IF NOT EXISTS btree_gist;
