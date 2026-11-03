// Supabase project "progetti" (schema `birds`).
// Locally, `make web-dev` serves its own config.js with a local database and token.
window.APP_CONFIG = {
  supabaseUrl: "https://jbpdyxztnljbfowrnsih.supabase.co",
  // Project Settings -> API Keys -> "Publishable key" (sb_publishable_...). Public on purpose:
  // the `birds` schema and the bird-audio bucket are open to it (supabase/birds_schema.sql), so
  // whoever has the site's link reads and writes. Never put a secret key here.
  supabaseKey: "sb_publishable_92agUxG_-8nUUuX7H2bzJQ_xiXFPGuW",
  schema: "birds",
  // the iNaturalist user whose observations the Report shows (inat-widget)
  inatUser: "andreaadelfio"
};
