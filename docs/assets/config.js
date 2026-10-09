// Supabase project "progetti" (schema `birds`).
// Locally, `make web-dev` serves its own config.js with a local database and token.
window.APP_CONFIG = {
  supabaseUrl: "https://jbpdyxztnljbfowrnsih.supabase.co",
  // Project Settings -> API Keys -> "Publishable key" (sb_publishable_...): safe in a public
  // site because the `birds` schema gives `anon` no access; you must log in.
  supabaseKey: "sb_publishable_92agUxG_-8nUUuX7H2bzJQ_xiXFPGuW",
  schema: "birds"
};
