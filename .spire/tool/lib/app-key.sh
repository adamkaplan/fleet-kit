#!/usr/bin/env bash
# The fleet GitHub App's private key (FLEET_APP_PRIVATE_KEY, names: app-env.mjs) is read by the lander and by nothing else. Sourced, never run.
# app_key_take moves it OUT of the environment into the shell variable fleet_app_key (not exported), so no child of this process (a check, the council, a hook, a test) inherits it.
# The one place that hands it on is `FLEET_APP_PRIVATE_KEY=$fleet_app_key node app-push.mjs ...`: the environment of that single command.
app_key_take() { fleet_app_key=${FLEET_APP_PRIVATE_KEY-}; unset FLEET_APP_PRIVATE_KEY; }
