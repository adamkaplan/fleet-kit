#!/usr/bin/env node
// council-route.mjs: `land` runs it first, once. When the caller's environment gives the council no model route it says so on stderr, at the start and loudly, with the exact variable's NAME
// (never a value). It stops nothing: the council is advice only, and it records each member as unable to review with the same name. Exit 0 always.
import { routeProblem } from "./council-lib.mjs";

const p = routeProblem(process.env);
if (p) {
  console.error(`land: ERROR: the council cannot answer: ${p.text}. The landing goes on: each member is recorded as unable to review in the Land-Council trailer.`);
}
