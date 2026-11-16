// The files a runner's output says failed (kept for `land --retest`): `test at FILE:L:C` (a failing test) and `✖ FILE (Nms)` (a file that failed whole, a timeout included), relative to the step's directory.
export const failedFilesOf = (out) => {
  const lines = out.split("\n");
  const files = new Set();
  lines.forEach((l, i) => {
    const at = /^test at (\S+?):\d+:\d+$/.exec(l)?.[1];
    // a `todo` test that fails is marked and does not fail the run: it is not a failed file. The spec reporter of the pinned Node prints, right after its `test at FILE:L:C` line,
    // `✖ title (5329.7ms) # the todo's reason` for it (seen on a real run: `✖ routed local hosts ... (5329.716886ms) # a failed create ...`) and `✖ title (3ms)` for a real failure;
    // Node 26 marks it `⚠ title (3ms) # reason` instead. failed-files.test.mjs pins both.
    if (at && !/^[✖⚠] .*\(\d+(\.\d+)?ms\) # /.test(lines[i + 1] ?? "")) {
      files.add(at);
    }
    const whole = /^✖ (\S+\.test\.[cm]?[jt]sx?) \(/.exec(l)?.[1];
    if (whole) {
      files.add(whole);
    }
  });
  return [...files];
};
