// app-env.mjs: the names the fleet's GitHub App is configured by, in ONE place (the shell side is lib/app-key.sh, which names the key once).
// The land process gets them from its environment (`doppler run -p fleet -c <config> -- spire land`). The id is not a secret; the key is, and only the lander reads it (app-push.mjs).
export const APP_ID_ENV = "FLEET_APP_ID";
export const APP_KEY_ENV = "FLEET_APP_PRIVATE_KEY";
export const API_URL_ENV = "GITHUB_API_URL"; // default https://api.github.com
export const SERVER_URL_ENV = "GITHUB_SERVER_URL"; // default https://github.com (where `git push` goes)

/** App mode is ON when the id is set: a host without the app keeps its own identity; a host with it never falls back to one (there is no second switch). */
export const appMode = (env = process.env) => (env[APP_ID_ENV] ?? "").trim() !== "";

// The commit statuses (janus-infra/janus-platform#1009; statuses.mjs): while they are rolled out the attestation is written twice, as the commit-message trailers and as the statuses land/checks and land/council posted by the App.
// FLEET_LAND_STATUSES (read by `push`, the one place): unset = post them in app mode and, when the App may not, say so once and land by the trailers | `required` = an App that may not post STOPS the landing before main moves | `off` = the trailers alone, as before.
export const TARGET_URL_ENV = "FLEET_LAND_STATUS_URL"; // optional: the target_url of the statuses (where the run's full record is kept)
