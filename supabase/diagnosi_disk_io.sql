-- Chi consuma il Disk IO del progetto "progetti" (Bird Audio Suite, MyFinance, ISIN Monitor,
-- Listino: tutti nello stesso database). Solo letture: si incolla nell'SQL Editor di Supabase e si
-- lanciano una alla volta. Le statistiche contano da stats_reset (query 4).

-- 1. Le query che muovono più blocchi (letti dal disco, scritti, file temporanei)
select left(regexp_replace(query, '\s+', ' ', 'g'), 140) as query,
       calls,
       round(total_exec_time::numeric / 1000, 1)       as secondi,
       shared_blks_read                                 as letti_da_disco,
       shared_blks_dirtied + shared_blks_written        as scritti,
       temp_blks_read + temp_blks_written               as temporanei,
       pg_size_pretty(wal_bytes)                        as wal
from extensions.pg_stat_statements
order by shared_blks_read + shared_blks_dirtied + shared_blks_written + temp_blks_read + temp_blks_written desc
limit 20;

-- 2. Le query che scrivono più WAL (insert, update, upsert ripetuti)
select left(regexp_replace(query, '\s+', ' ', 'g'), 140) as query,
       calls, rows, pg_size_pretty(wal_bytes) as wal, wal_records
from extensions.pg_stat_statements
order by wal_bytes desc
limit 15;

-- 3. Le tabelle: letture complete (seq_scan) e righe scritte, di ogni schema (birds, public, storage, ...)
select schemaname, relname, seq_scan, seq_tup_read, idx_scan,
       n_tup_ins, n_tup_upd, n_tup_hot_upd, n_tup_del, n_dead_tup,
       autovacuum_count, pg_size_pretty(pg_total_relation_size(relid)) as dimensione
from pg_stat_user_tables
order by n_tup_ins + n_tup_upd + n_tup_del + seq_tup_read / 100 desc
limit 20;

-- 4. Il database intero: file temporanei (ordinamenti che non stanno in memoria), blocchi letti
-- dal disco contro quelli trovati in memoria, transazioni, e da quando si conta
select temp_files, pg_size_pretty(temp_bytes) as temporanei, blks_read, blks_hit,
       xact_commit, stats_reset,
       (select stats_reset from extensions.pg_stat_statements_info) as statements_da
from pg_stat_database
where datname = current_database();
