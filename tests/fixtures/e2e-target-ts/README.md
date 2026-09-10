# e2e-target-ts

Node.js 20 / TypeScript twin of `e2e-target` for the SC-20 end-to-end
scenario (WS-07 WP 11.2): a tiny calculator whose `divide` is deliberately
missing, so a scripted agent can be asked to implement it.

Run the tests with `npx jest --ci` (JUnit XML via the `jest-junit` reporter,
written to the path in `JEST_JUNIT_OUTPUT_FILE`).

The baseline suite is green by construction; the scenario asks the agent to
add `divide` to `src/calculator.ts` plus a test.
