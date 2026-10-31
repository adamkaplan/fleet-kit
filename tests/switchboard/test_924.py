"""The tests PR #924 added to bin/test-switchboard, moved out of it (its line ceiling): whole added methods and functions, text unchanged.

The fixtures and the base classes are those of bin/test-switchboard (HOST, set by its loader block). The moved methods of TestPR8Source and TestImpactHold are
bound on two classes of their own that inherit the rest; the inherited tests are switched off there (None), so each test runs once, on its own class."""

HOST = None   # set by bin/test-switchboard


def load():
    g = globals()
    for key, value in vars(HOST).items():   # the code below reads the names of the entry file as its globals
        if not key.startswith("__"):
            g.setdefault(key, value)
    TestPR8Source, TestImpactHold = HOST.TestPR8Source, HOST.TestImpactHold

    def hostile_parser_cases():
        """(label, mutator(parser)) for each thing a hostile `build_parser` can do to the shared parser of `review record` and `impact clear` (#924). One table: the two tests that use it (the record and the clear)
        and the completeness test read the same list. A mutator gets the REAL parser `build_parser` built and changes it; the hostile build_parser is then the real one plus that change."""
        def subparser(parser, *path):
            for name in path:
                (top,) = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
                parser = top.choices[name]
            return parser

        def hostile_for(path):   # `impact_action` is the dest `impact`'s own subcommand is dispatched by: a default on it would break that dispatch, not the clear's reading
            return {k: v for k, v in HOSTILE_RATING_DEFAULTS.items() if not (path[0] == "impact" and k == "impact_action")}

        def defaults_on(*path):
            return lambda parser: subparser(parser, *path).set_defaults(**hostile_for(path))

        def replaced(parser):   # a fresh `review` parser with hostile defaults, put in the subparsers' table in place of the real one
            (top,) = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
            fresh = argparse.ArgumentParser().add_subparsers(dest="action")
            sb.add_review_parsers(fresh)
            review = fresh.choices["review"]
            (inner,) = [a for a in review._actions if a.__class__.__name__ == "_SubParsersAction"]
            inner.choices["record"].set_defaults(**HOSTILE_RATING_DEFAULTS)
            top.choices["review"] = review
            top._name_parser_map["review"] = review

        def same_dest(*path):
            return lambda parser: subparser(parser, *path).add_argument("--zz", dest="impact_severity", default="Negligible")

        def reparsed(parser):   # the namespace is parsed again (and rewritten) after the real parse
            original = parser.parse_known_args

            def wrapper(args=None, namespace=None):
                result = original(args, namespace)
                for key, value in hostile_for(("impact",) if list(args or [])[:1] == ["impact"] else ("review",)).items():
                    setattr(result[0], key, value)
                return result
            parser.parse_known_args = wrapper

        # (`set_defaults` on the parent `review` parser is not in the table: argparse never lets it reach a record's namespace, so it is not a hostile act.) The extra flag only matters when the line carries it: `HOSTILE_EXTRA_ARGV`
        return [("set_defaults on review record", defaults_on("review", "record")), ("set_defaults on impact clear", defaults_on("impact", "clear")),
                ("a replaced review subparser", replaced), ("an extra same-dest flag on review record", same_dest("review", "record")),
                ("an extra same-dest flag on impact clear", same_dest("impact", "clear")), ("a re-parsed namespace", reparsed)]

    HOSTILE_EXTRA_ARGV = {"an extra same-dest flag on review record": ["--zz", "Negligible"], "an extra same-dest flag on impact clear": ["--zz", "Negligible"]}   # the line the hostile flag is typed on

    HOSTILE_RAN = []   # one entry each time a hostile `build_parser` of the table is really called: the proof that a test ran it (the direct route never does)

    def hostile_build_parser(mutator):
        real = sb.build_parser

        def build():
            HOSTILE_RAN.append(1)
            parser = real()
            mutator(parser)
            return parser
        return build

    def _hostile_clear_test(index):
        def test(self):
            label, mutator = hostile_parser_cases()[index]
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            line = ["impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity", "--brief-digest", self.FIXTURE_BRIEF["digest"], "--brief-text", self.FIXTURE_BRIEF["text"], *(HOSTILE_EXTRA_ARGV.get(label, []) if "impact clear" in label else [])]
            del HOSTILE_RAN[:]
            os.environ["HERDR_PANE_ID"] = self.world.panes["cos"]
            with captured() as (o, e), mock.patch.object(sb, "owner_terminal_problem", lambda *a, **k: self.terminal_problem):   # the hostile parser REALLY runs; its namespace reaches the real clear
                args = hostile_build_parser(mutator)().parse_known_args(line)[0]
                args.raw_argv = tuple(line)
                code = sb.cmd_impact(args, self.route, self.world.clock.now)
            err = e.getvalue()
            self.assertEqual(HOSTILE_RAN, [1], "the hostile build_parser really ran")
            deletes = sorted(w[1].rsplit("/", 1)[1] for w in self.writes if w[0] == "DELETE")
            cleared = self.events("gate.hold_cleared")
            self.assertEqual(code, 0, "%s: %s" % (label, err))   # the same clear as with the real parser (test_s1): the order, the head and the labels of the command line and the ledger
            (event,) = cleared
            detail = event["detail"]
            self.assertEqual((detail["by"], detail["under_order"], detail["head"], detail["via"]), ("cos", 17, LEDGER_HEAD, "order:17"), label)
            self.assertTrue(deletes, label)
        return test

    def _hostile_registrar_clear_test(label):
        def test(self):
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            calls = []
            registrar = self.hostile_registrars_by_probe(calls)[label]
            with mock.patch.object(sb, "add_zzz_parsers", registrar, create=True):
                code, out, err = self.under()
            self.assertEqual((code, calls), (0, []), err)   # the registrar is never called; the clear is the clean one
            (event,) = self.events("gate.hold_cleared")
            self.assertEqual((event["detail"]["under_order"], event["detail"]["head"], event["detail"]["via"]), (17, LEDGER_HEAD, "order:17"))
            self.assertTrue([w for w in self.writes if w[0] == "DELETE"])
        return test

    class TestPR8Source924(TestPR8Source):
        """The tests of #924 that were added to TestPR8Source, moved from bin/test-switchboard."""

        def test_a_name_defined_twice_still_fails_the_scan_whatever_the_two_definitions_are(self):
            for label, text in (("def and def", "def f():\n    pass\ndef f():\n    pass\n"), ("def and assign", "def f():\n    pass\nf = 1\n"),
                                ("assign and def", "f = 1\ndef f():\n    pass\n"), ("assign and assign", "f = 1\nf = 2\n"), ("class and assign", "class f:\n    pass\nf = 2\n"),
                                ("def, del, def", "def f():\n    pass\ndel f\ndef f():\n    pass\n")):
                with self.subTest(case=label):
                    self.assertEqual([x[0] for x in self.names_defined_twice(text)], ["f"])
            self.assertEqual(self.names_defined_twice("def f():\n    pass\ndel f\n"), [], "a del alone is not a second definition")

        def names_the_guard_is_built_from(self):
            """{name: (the part file, the first top-level statement that reads it after its definition)}, DERIVED from the daemon's source: the names the guard is built from (the arguments of `make_effect_guard(...)` and of `.bind(...)`)
            and every name of the decorator of an `effect_guard(...)` effect, that the daemon defines at top level. The maker is not one (it is deleted by the daemon itself, and a test elsewhere holds it unreachable)."""
            trees = {path: ast.parse(path.read_text()) for path in SWITCHBOARD_SOURCE_FILES}
            defined = {}
            for path, tree in trees.items():
                for node in tree.body:
                    for each in ([node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else [t.id for t in (node.targets if isinstance(node, ast.Assign) else []) if isinstance(t, ast.Name)]
                                 + [e.id for t in (node.targets if isinstance(node, ast.Assign) else []) if isinstance(t, ast.Tuple) for e in t.elts if isinstance(e, ast.Name)]):
                        defined.setdefault(each, (path, node.lineno))
            wanted = set()
            for tree in trees.values():
                for node in tree.body:
                    for call in ast.walk(node):
                        if isinstance(call, ast.Call) and getattr(call.func, "id", getattr(call.func, "attr", "")) in ("make_effect_guard", "bind") and (getattr(call.func, "id", "") == "make_effect_guard" or isinstance(getattr(call.func, "value", None), ast.Call)):   # `load_module("impact_hold").bind(...)`, not a socket's
                            wanted |= {x.id for a in call.args for x in ast.walk(a) if isinstance(x, ast.Name)}
                    if isinstance(node, ast.FunctionDef):
                        for decorator in node.decorator_list:
                            if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "effect_guard":
                                wanted |= {x.id for x in ast.walk(decorator) if isinstance(x, ast.Name)}
            found = {}
            for name in sorted(wanted - {"make_effect_guard"}):
                if name not in defined or name in vars(__import__("builtins")):
                    continue
                where, line = defined[name]
                for path in SWITCHBOARD_SOURCE_FILES:
                    for node in trees[path].body:
                        if (path, node.lineno) > (where, line) and SWITCHBOARD_SOURCE_FILES.index(path) >= SWITCHBOARD_SOURCE_FILES.index(where) and any(
                                isinstance(x, ast.Name) and x.id == name and isinstance(x.ctx, ast.Load) for x in ast.walk(node)) and not (path == where and node.lineno <= line):
                            found.setdefault(name, (path, min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])))   # a decorated def starts at its first decorator
                            break
                    if name in found:
                        break
            return found

        def daemon_with_line_before_first_use(self, name, where):
            """A copy of the entry, its manifest and its parts with `del <name>` as a new statement just before the first statement that reads `name` after its definition; loaded by the real loader."""
            path, lineno = where
            with tempfile.TemporaryDirectory() as directory:
                copy_entry(directory)
                for helper in BIN.glob("*.py"):   # the helper modules the entry loads by path from beside itself
                    shutil.copy(str(helper), str(Path(directory) / helper.name))
                part = Path(directory) / path.relative_to(BIN)
                lines = part.read_text().split("\n")
                if name:
                    lines.insert(lineno - 1, "del %s" % name)
                part.write_text("\n".join(lines))
                return _load("t_mutant_%s" % name, Path(directory) / "fleet-switchboard")

        def test_the_names_the_guard_is_built_from_are_derived_from_the_code(self):
            self.assertTrue({"effect_guard", "EFFECT_CHECK"} <= set(self.names_the_guard_is_built_from()), sorted(self.names_the_guard_is_built_from()))

        def test_a_del_of_a_name_the_guard_is_built_from_stops_the_daemon_from_loading_for_every_such_name(self):
            """The hostile line: `del <name>` (the effect_guard, the latch's check, anything an effect's decorator reads) injected into the real daemon source before its first use. The daemon must not load (the effect would run unguarded or not at all);
            the control loads the same copy with no line. A `del` is not a second definition for the scan, so the scan above does not stand for this: this test does."""
            names = self.names_the_guard_is_built_from()
            self.assertGreaterEqual(len(names), 2)
            control = self.daemon_with_line_before_first_use(None, next(iter(names.values())))
            self.assertTrue(callable(control.effect_guard) if hasattr(control, "effect_guard") else callable(control.cmd_impact), "the copy with no injected line loads")
            for name, where in sorted(names.items()):
                with self.subTest(deleted=name):
                    with self.assertRaises((NameError, SystemExit)) as raised:
                        self.daemon_with_line_before_first_use(name, where)
                    self.assertIn(name, str(raised.exception), "it stops for the deleted name, not for something else")

    for _name in dir(TestPR8Source):   # the inherited tests run on TestPR8Source
        if _name.startswith("test") and _name not in vars(TestPR8Source924):
            setattr(TestPR8Source924, _name, None)


    class TestImpactHold924(TestImpactHold):
        """The tests of #924 that were added to TestImpactHold, moved from bin/test-switchboard."""

        @property
        def RATING_ROOT_WHY(self):   # one row for each of `rating_roots`, in the matrix (`rating_roots_why`): the one table the code, the matrix text and docs/switchboard.md share
            return sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]["rating_roots_why"]

        def rating_path_derived(self):
            """The definitions that belong to the rating's path, derived from the code: the call path from `main` to a rating target (with the two dynamic edges), the definitions that read, write or register a rating field and the
            the definitions `main` itself names (its guard and the dispatch). (names, the parsed daemon, the matrix rule)."""
            import ast
            rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
            module = sb.load_module("delegated_clear")
            daemon = ast.parse(self.real_source())
            defs = {n.name: n for n in daemon.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
            dynamic = rule["dynamic_actions"]
            fields = set(sb.IMPACT_RATING_FIELDS) | {"by_role"}
            names = {"IMPACT_RATING_FIELDS", "IMPACT_CLASS_RE"}
            touching = {n for n, node in defs.items() if isinstance(node, ast.FunctionDef) and any((isinstance(x, ast.Constant) and (x.value in fields or (isinstance(x.value, str) and re.fullmatch(r"--impact-(severity|probability|reason|class|action)", x.value)))) or (isinstance(x, ast.Name) and x.id in names) for x in ast.walk(node))}
            from_main = module.closure(daemon, ["main"], dynamic)[0]
            def edges(node):
                out = {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and x.id in defs}
                if any(isinstance(x, ast.Constant) and x.value == "cmd_" for x in ast.walk(node)):
                    out |= {"cmd_" + a for a in dynamic}
                if any(isinstance(x, ast.Name) and x.id == "REGISTRAR_NAME_RE" for x in ast.walk(node)):
                    out |= {"add_%s_parsers" % a for a in dynamic}
                return out
            graph = {n: edges(defs[n]) for n in from_main if n in defs}
            on_path = {"cmd_review", "parse_review_block", "add_review_parsers"}
            while True:
                more = {n for n, out in graph.items() if out & on_path} - on_path
                if not more:
                    break
                on_path |= more
            clear_dispatchers = {n for n, node in defs.items() if isinstance(node, ast.FunctionDef) and any(isinstance(x, ast.Name) and x.id == "cmd_impact_clear" for x in ast.walk(node))}   # the clear's own entry: the definition that names it (`cmd_impact`) runs it, though `main` reaches it only by the string "cmd_" + action, which the walker follows for the rating's action only
            path = (on_path & set(from_main)) | clear_dispatchers | touching | {x.id for fn in ("main", "private_parser") for x in ast.walk(defs[fn]) if isinstance(x, ast.Name) and x.id in defs}   # what main and the private parser name themselves
            handing = {c.func.id for n in path if n in defs for c in ast.walk(defs[n]) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in defs and any(k.arg is None for k in c.keywords)}   # what the path hands the handler's arguments to (`call_command(handler, **available)`)
            return path | handing, daemon, rule

        def test_the_rating_roots_are_derived_from_the_code_and_each_is_classified_in_one_table(self):
            module = sb.load_module("delegated_clear")
            derived, daemon, rule = self.rating_path_derived()
            dynamic = rule["dynamic_actions"]
            self.assertEqual(sorted(rule["rating_roots"]), sorted(self.RATING_ROOT_WHY), "rating_roots and the classification table name the same roots: a root added to the code path is classified here (or not a root)")
            self.assertTrue(all(len(why) > 20 for why in self.RATING_ROOT_WHY.values()))
            self.assertEqual(sorted(set(rule["rating_roots"]) - derived), [], "every root is on the rating's path (derived from the code)")
            reach = module.closure(daemon, module.daemon_roots(rule), dynamic)[0]
            self.assertEqual(sorted(n for n in derived if n not in reach), [], "every definition on the rating's path is in the closure")
            for root in sorted(self.RATING_ROOT_WHY):   # one mutant for each member: without this root, the path is still covered (it is also named by another root, said here) or the derived set leaves the closure
                with self.subTest(root=root):
                    rest = dict(rule, rating_roots=[r for r in rule["rating_roots"] if r != root])
                    lost = sorted(n for n in derived if n not in module.closure(daemon, module.daemon_roots(rest), dynamic)[0])
                    self.assertEqual(lost, [], "%s is also reached by name from another root (main), so the list names it explicitly to hold it if that path changes" % root) if root not in ("main", "cmd_impact") else self.assertNotEqual(lost, [], "main and cmd_impact are roots no other root reaches by name: without them the path leaves the closure; the others are reached from")
            self.assertTrue({"code_snapshot", "code_replaced", "direct_command", "private_parser", "add_impact_parsers"} <= derived & set(rule["rating_roots"]), "the guard main runs is a derived root (the definitions main names itself)")
            hand_list = "RATING_PATH" + "_FUNCTIONS"   # the hand-kept list of the old guard: gone, nothing to pin
            self.assertEqual([f.name for f in list((REPO / "bin").glob("*")) + list((REPO / "docs").glob("*.md")) if f.is_file() and f.name != "test-switchboard" and hand_list in f.read_text(errors="replace")], [])
            self.assertEqual(sorted(n for n in derived if n.startswith("add_") and n not in ("add_review_parsers", "add_impact_parsers")), [], "no other registrar is on the rating's path (derived): the two of the private parser")

        CORE = "add_core_parsers"

        def core_statements(self):
            """The (first line number, text) of every statement of the moved registrar `add_core_parsers`, derived from the code."""
            import ast
            node = next(n for n in ast.parse(self.real_source()).body if isinstance(n, ast.FunctionDef) and n.name == self.CORE)
            return [(s.lineno, s.end_lineno) for s in node.body[1:]]

        def test_a_flag_or_a_subcommand_of_another_command_is_not_clear_machinery_every_statement_of_the_registrar_and_the_four_shapes(self):
            import difflib
            lines = self.real_source().split("\n")
            statements = self.core_statements()
            self.assertGreater(len(statements), 50, "the moved registrar holds the other commands' registration")
            def check(head_lines):
                diff = "\n".join(list(difflib.unified_diff(lines, head_lines, lineterm="", n=3))[2:])
                return self.closure_check("\n".join(head_lines), diff)
            head = list(lines)   # derived: a comment on EVERY statement of the registrar, in one edit (one closure walk)
            for first, last in statements:
                head[first - 1] = head[first - 1] + "  # x"
            self.assertEqual(check(head), [], "every line of the other commands' registration may change")
            def after(prefix, new):
                i = next(k for k, l in enumerate(lines) if l.startswith(prefix))
                return lines[:i + 1] + [new] + lines[i + 1:]
            shapes = {"a flag added to send": after('    send.add_argument("--from"', '    send.add_argument("--zz", help="x")'),
                      "a flag renamed on status": [l.replace('status.add_argument("--json"', 'status.add_argument("--jsn"') for l in lines],
                      "a flag removed from run": [l for l in lines if not l.startswith('    run.add_argument("--once"')],
                      "a subcommand added": after('    sub.add_parser("poke"', '    sub.add_parser("zzz", help="x")')}
            for label, head in shapes.items():
                with self.subTest(shape=label):
                    self.assertNotEqual(head, lines)
                    self.assertEqual(check(head), [], label)

        def test_the_wiring_that_stays_in_build_parser_and_the_rating_path_stay_refused_and_a_mutant_that_re_adds_the_registrar_to_the_path_is_caught(self):
            import difflib
            module = sb.load_module("delegated_clear")
            rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
            lines = self.real_source().split("\n")
            def edit(head):
                return "\n".join(list(difflib.unified_diff(lines, head, lineterm="", n=3))[2:])
            for prefix in ("    for registrar in command_registrars():", "    parser.commands = frozenset", "    sub = parser.add_subparsers("):
                with self.subTest(wiring=prefix):
                    i = next(k for k, l in enumerate(lines) if l.startswith(prefix))
                    head = list(lines)
                    head[i] = head[i] + "  # x"
                    self.assertTrue(self.closure_check("\n".join(head), edit(head)), prefix)
            flag = next(k for k, l in enumerate(lines) if l.startswith('    send.add_argument("--from"'))
            loop = next(k for k, l in enumerate(lines) if l.startswith("    for registrar in command_registrars():"))
            # the mutant: build_parser names the registrar again (the removed root back on the path). The flag edit that passes on the real file is refused on it
            mutant = list(lines)
            mutant.insert(loop, "    add_core_parsers(sub)")
            mutant_flag = next(k for k, l in enumerate(mutant) if l.startswith('    send.add_argument("--from"'))
            head = list(mutant)
            head[mutant_flag] = head[mutant_flag] + "  # x"
            diff = "\n".join(list(difflib.unified_diff(mutant, head, lineterm="", n=3))[2:])
            refused = module.closure_refusals("\n".join(mutant), "\n".join(head), diff, module.daemon_roots(rule), set(rule["clear_entry_points"]), False, rule["dynamic_actions"])
            self.assertTrue(refused, "with build_parser naming the registrar, a flag of send is on the clear's path again")
            real_head = list(lines)
            real_head[flag] = real_head[flag] + "  # x"
            self.assertEqual(self.closure_check("\n".join(real_head), edit(real_head)), [], "and on the real file it is not")
            reach = module.closure(__import__("ast").parse("\n".join(mutant)), module.daemon_roots(rule), rule["dynamic_actions"])[0]
            self.assertIn(self.CORE, reach)
            # an edit of the same line, left in build_parser instead of the registrar (the rebase mistake): refused as wiring
            in_build = list(lines)
            j = next(k for k, l in enumerate(in_build) if l.startswith("    for registrar in command_registrars():"))
            in_build.insert(j, '    sub.add_parser("zzz", help="x")')
            self.assertTrue(self.closure_check("\n".join(in_build), edit(in_build)), "a subcommand added in build_parser is refused: it belongs in the registrar")

        def test_the_docs_and_the_matrix_say_the_same_roots_with_the_same_reasons_and_the_clear_holds_no_root_name(self):
            rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
            docs = (REPO / "docs" / "switchboard.md").read_text()
            section = docs[docs.index("#### Which roots remain and why (#924)"):]
            section = section[:section.index("\n#### ", 5)] if "\n#### " in section[5:] else section
            rows = dict(re.findall(r"^\| `([a-z_]+)` \| (.+) \|$", section, re.M))
            self.assertEqual(rows, rule["rating_roots_why"], "docs/switchboard.md lists the matrix's roots with the matrix's words (0 differences)")
            self.assertEqual(sorted(rows), sorted(rule["rating_roots"]))
            self.assertIn("rating_roots_why", rule["meaning"])   # the matrix text names the table of reasons
            source = (BIN / "delegated_clear.py").read_text()
            for name in set(rule["rating_roots"]) | set(rule["closure_roots"]):
                if len(name) > 4:
                    self.assertNotIn('"%s"' % name, source, "delegated_clear.py holds no name of the daemon file's roots (the matrix does)")

        def parser_surface(self, parser):
            """The full parsed surface of a parser: one row for each action at every depth (path, option strings, dest, nargs, type name, default, choices, required, help, class)."""
            rows = []
            def walk(p, path):
                for action in p._actions:
                    rows.append((path, tuple(action.option_strings), action.dest, str(action.nargs), getattr(action.type, "__name__", str(action.type)), repr(action.default),
                                 repr(sorted(map(str, action.choices))) if action.choices and action.__class__.__name__ != "_SubParsersAction" else "", bool(getattr(action, "required", False)), action.help, action.__class__.__name__))
                    if action.__class__.__name__ == "_SubParsersAction":
                        for name, sub in action.choices.items():
                            walk(sub, path + (name,))
            walk(parser, ())
            return sorted(rows, key=repr)

        def test_the_whole_parsed_surface_is_walked_each_command_is_registered_once_and_every_add_parser_is_seen(self):
            """#924, the pure move: NOTE the pin of a dropped flag or command is the per-command help fixture test (`test_every_subcommand_of_the_snapshot_still_exists_with_the_same_help`); this walk holds the invariants of the shape (each command once, nested flags present). The full surface:  (every subparser at every depth) is walked from the real `build_parser()`. Nothing is compared with a stored copy (the per-command help fixtures and this walk are the
            proof the move changed nothing; the before and after equality is measured on the base commit's text in the pull request)."""
            import ast
            parser = sb.build_parser()
            rows = self.parser_surface(parser)
            (top,) = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
            top_names = [r[0][0] for r in rows if len(r[0]) == 1 and r[9] == "_HelpAction" and r[2] == "help"]
            self.assertEqual(sorted(top_names), sorted(top.choices), "each registered command appears once in the walk")
            self.assertEqual(len(top_names), len(set(top_names)))
            self.assertEqual(sorted(top.choices), sorted(parser.commands))
            registrar_calls = sum(1 for n in ast.walk(ast.parse(self.real_source())) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_parser" and not any(isinstance(a, ast.Name) for a in n.args[:1]))
            seen = {r[0] for r in rows if r[9] == "_HelpAction"}
            self.assertGreaterEqual(len(seen) - 1, registrar_calls - 5, "every add_parser call (a literal name) is a parser in the walk")
            for command in ("send", "status", "run", "report", "notice", "ensure", "poke", "launch", "pending", "remind", "version"):
                self.assertIn(command, top.choices)
            self.assertIn((("review", "record"), ("--impact-severity",)), {(r[0], r[1]) for r in rows}, "a nested flag is in the walk (the help fixtures do not see it)")
            core = ast.parse(self.real_source())
            node = next(n for n in core.body if isinstance(n, ast.FunctionDef) and n.name == self.CORE)
            names = {c.args[0].value for c in ast.walk(node) if isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "add_parser" and c.args and isinstance(c.args[0], ast.Constant)}
            self.assertTrue(names <= set(top.choices) and len(names) >= 10, sorted(names))

        def test_a_hostile_build_parser_changes_nothing_in_the_recorded_rating_one_case_for_each_thing_it_can_do(self):
            """#924: the real `main()` with the real `build_parser` wrapped by each hostile change of `hostile_parser_cases` (one table, also read by the clear's test and the completeness test): `review record` writes exactly
            what the command line says (the entry of a clean run), or fails closed (a non-zero exit and nothing written)."""
            cases = hostile_parser_cases()
            self.assertEqual(len({label for label, _ in cases}), len(cases))
            self.record_pr_setup()
            for flags_label, flags in (("explicit flags", self.EXPLICIT), ("no flags", ())):
                clean_code, clean_err, clean = self.record_run(flags)
                self.assertEqual(clean_code, 0, clean_err)
                for label, mutator in cases:
                    with self.subTest(flags=flags_label, hostile=label):
                        del HOSTILE_RAN[:]
                        code, err, entry = self.record_run(tuple(flags) + tuple(HOSTILE_EXTRA_ARGV.get(label, [])) if "review record" in label or label not in HOSTILE_EXTRA_ARGV else flags, build=hostile_build_parser(mutator), namespace=True)
                        self.assertEqual(HOSTILE_RAN, [1], "the hostile build_parser really ran")
                        self.assertEqual((code, entry), (0, clean), "the record is exactly what the command line says: %r" % (err,))

        def test_the_direct_route_never_calls_build_parser_for_the_record(self):
            """The direct route, named as that (#924): `review record` run through the real `main` takes the private parser, so a hostile `build_parser` is NEVER called, and the record is the clean one."""
            self.record_pr_setup()
            clean_code, clean_err, clean = self.record_run(self.EXPLICIT)
            del HOSTILE_RAN[:]
            for label, mutator in hostile_parser_cases():
                with self.subTest(label):
                    code, err, entry = self.record_run(self.EXPLICIT, build=hostile_build_parser(mutator))
                    self.assertEqual((code, entry, HOSTILE_RAN), (0, clean, []), err)

        def test_the_table_of_hostile_parsers_really_changes_the_shared_parser(self):
            """Completeness of the table: each hostile mutator is effective (the namespace of the shared parser DOES carry a hostile value), so a pass of the two tests above is not a no-op."""
            for label, mutator in hostile_parser_cases():
                with self.subTest(hostile=label):
                    parser = hostile_build_parser(mutator)()
                    argv = (["impact", "clear", "acme/app#9", "--reason", "a reason long enough"] if "clear" in label else ["review", "record", "acme/app#9", "--reviewer", "impact", "--verdict", "approve"]) + HOSTILE_EXTRA_ARGV.get(label, [])
                    args, _ = parser.parse_known_args(argv)   # no rating flag on the line: the hostile default is what the shared namespace carries
                    self.assertEqual(getattr(args, "impact_severity", None), "Negligible", label)

        def test_a_hostile_build_parser_cannot_make_the_real_rating_path_read_the_namespace(self):
            """The mutant of #924's immunity: the record read from the shared namespace instead of the raw line gives the hostile value, so the first test above would fail."""
            self.record_pr_setup()
            label, mutator = hostile_parser_cases()[0]
            real = sb.rating_inputs
            def reads_namespace(raw_argv):   # the mutant: the shared parser's namespace is the source
                args, _ = hostile_build_parser(mutator)().parse_known_args(list(raw_argv))
                return {k: getattr(args, k) for k in sb.RATING_INPUTS if k != "block" and getattr(args, k, None) is not None}   # (the hostile `block` default would only make the record refuse)
            del HOSTILE_RAN[:]
            with mock.patch.object(sb, "rating_inputs", reads_namespace):
                code, err, entry = self.record_run((), build=hostile_build_parser(mutator), namespace=True)
            self.assertGreaterEqual(len(HOSTILE_RAN), 2, "the hostile build_parser ran (to build the namespace, and inside the mutant)")
            self.assertEqual((code, entry["impact_severity"]), (0, "Negligible"), "the mutant records the hostile default: the record test above would fail on it")
            clean_code, _, clean = self.record_run(())
            self.assertNotIn("impact_severity", clean)

        def guarded_names(self):
            """Every function the clear and the rating run by, DERIVED from the closure (never a list): (the daemon file's definitions in the closure of the matrix's roots, the functions of the helper modules the clear is made of)."""
            import ast, inspect
            module = sb.load_module("delegated_clear")
            rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
            daemon = ast.parse(self.real_source())
            reach = module.closure(daemon, module.daemon_roots(rule), rule["dynamic_actions"])[0]
            functions = {n.name for n in daemon.body if isinstance(n, ast.FunctionDef) and n.name in vars(sb)}   # a factory deleted after its one call (`make_cmd_impact`) is not a name a registrar can reach
            daemon_names = sorted(n for n in reach if n in functions)
            helper = {}
            for name in ("impact_hold", "delegated_clear"):
                mod = sb.load_module(name)
                helper[name] = sorted(n for n, v in vars(mod).items() if inspect.isfunction(v) and v.__module__ == mod.__name__)
            return daemon_names, helper

        def reassign_registrar(self, name, module=None):
            target = sb if module is None else sb.load_module(module)
            original = getattr(target, name)
            def same_behaviour(*a, **k):   # a DIFFERENT object that does what the original does: the harness keeps working, the guard must still see it
                return original(*a, **k)
            def registrar(sub):
                setattr(target, name, same_behaviour)
            return registrar

        def test_a_registrar_that_reassigns_any_function_the_clear_or_the_rating_runs_by_stops_the_command_for_every_action(self):
            """#924, the real `main()` with a real registrar that REASSIGNS one function (one case for each, derived from the closure by `guarded_names`): `review record` writes nothing, `impact clear` clears nothing, and any other action stops too."""
            daemon_names, helper = self.guarded_names()
            self.assertTrue({"cmd_review", "cmd_impact_clear", "dispatch", "rating_inputs", "impact_clear_core", "build_parser"} <= set(daemon_names) | set(helper["impact_hold"]) | {"build_parser"}, daemon_names[:5])
            self.no_direct_route()   # the BACKSTOP: the routes that still run registrars (the direct route runs none)
            self.record_pr_setup()
            for name in daemon_names:
                with self.subTest(record_function=name):
                    original = getattr(sb, name)
                    try:
                        code, err, entry = self.record_run(self.EXPLICIT, [("add_zzz_parsers", self.reassign_registrar(name))])
                    finally:
                        setattr(sb, name, original)
                    self.assertEqual((code, entry), (1, None), err)
                    self.assertIn("a command registrar replaced code a command runs by", err)

        def no_direct_route(self):
            """The pre-dispatch switched off: `review record` and `impact clear` take the normal route (every registrar runs), which is what the backstop guards."""
            patcher = mock.patch.object(sb, "direct_command", lambda raw: False)
            patcher.start()
            self.addCleanup(patcher.stop)

        def test_a_registrar_that_reassigns_a_function_of_the_clear_leaves_the_hold_as_it_was(self):
            daemon_names, helper = self.guarded_names()
            self.no_direct_route()
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            cases = [(name, None) for name in daemon_names] + [(name, module) for module, names in helper.items() for name in names]
            self.assertGreater(len(cases), 100)
            for name, module in cases:
                with self.subTest(function=name, module=module):
                    target = sb if module is None else sb.load_module(module)
                    original = getattr(target, name)
                    registrar = self.reassign_registrar(name, module)
                    patcher = mock.patch.object(sb, "add_zzz_parsers", registrar, create=True)
                    patcher.start()
                    try:
                        code, out, err = self.under()
                    finally:
                        patcher.stop()
                        setattr(target, name, original)
                    self.assertEqual(code, 1, err)
                    self.assertIn("a command registrar replaced code a command runs by", err)
                    self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [], "nothing was cleared")
                    self.assertEqual(self.events("gate.hold_cleared"), [])
            with self.subTest("the sys.modules one-liner"):   # the probe of the review: the registrar names the module through sys.modules
                def probe(sub):
                    vars(sb).update(cmd_impact_clear=lambda *a, **k: 0)   # the module's namespace, as `sys.modules[__name__].x = ...` writes it
                original = sb.cmd_impact_clear
                patcher = mock.patch.object(sb, "add_zzz_parsers", probe, create=True)
                patcher.start()
                try:
                    code, out, err = self.under()
                finally:
                    patcher.stop()
                    sb.cmd_impact_clear = original
                self.assertEqual(code, 1, err)
            snapshot = sb.code_snapshot()   # the kinds of binding the guard holds, DERIVED from the snapshot: each kind is tried below with the real clear
            kinds = {"a function": any(k[1] and k[0] == "" and isinstance(v[0], types.FunctionType) for k, v in snapshot.items()), "a class": any(k[0] == "" and isinstance(v[0], type) for k, v in snapshot.items()),
                     "a class member": any("." in k[0] for k in snapshot), "a module-level constant": any(k[0] == "" and isinstance(v[0], (tuple, str, int)) for k, v in snapshot.items()),
                     "a helper module": any(k[0] == "modules" for k in snapshot), "a proxy object": any(k[0] == "" and type(v[0]).__name__.startswith("_Lazy") for k, v in snapshot.items())}
            self.assertEqual([k for k, present in kinds.items() if not present], [], "every kind of binding the guard is said to hold exists in the snapshot (completeness)")
            original_entries = sb.impact_rating_entries
            def forge_ledger(sub):   # M1 of the review: the clear's score source is `plan_ledger.read_ledger`, an attribute set on the lazy proxy
                real = sb.plan_ledger.read_ledger
                sb.plan_ledger.read_ledger = lambda state: real(state)
            def rebind_constant(sub):
                sb.IMPACT_RATING_FIELDS = tuple(sb.IMPACT_RATING_FIELDS)[::-1]
            def rebind_module(sub):
                sb.impact_floor = sb.load_module("impact_floor")   # the module itself in place of the lazy proxy bound under this name
            def replace_class(sub):
                klass = next(v for n, v in sorted(vars(sb).items()) if isinstance(v, type) and v.__module__ == sb.__name__)
                setattr(sb, klass.__name__, type(klass.__name__, (klass,), {}))
            def load_new_module(sub):
                sb.load_module("lanes")
            for label, registrar, undo in (("a proxy attribute forged (the plan_ledger.read_ledger probe)", forge_ledger, lambda: sb.plan_ledger.__dict__.pop("read_ledger", None)),
                                           ("a module-level constant rebound", rebind_constant, None), ("a bound helper module replaced", rebind_module, None), ("a class replaced", replace_class, None),
                                           ("a helper module first loaded by the registrar", load_new_module, None)):
                with self.subTest(label):
                    saved = {n: getattr(sb, n) for n in ("IMPACT_RATING_FIELDS", "impact_floor")}
                    classes = {n: v for n, v in vars(sb).items() if isinstance(v, type) and v.__module__ == sb.__name__}
                    lanes = sys.modules.pop("lanes", None) if registrar is load_new_module else None
                    patcher = mock.patch.object(sb, "add_zzz_parsers", registrar, create=True)
                    patcher.start()
                    try:
                        code, out, err = self.under()
                    finally:
                        patcher.stop()
                        for n, v in saved.items():
                            setattr(sb, n, v)
                        for n, v in classes.items():
                            setattr(sb, n, v)
                        if undo:
                            undo()
                        if registrar is load_new_module:
                            sys.modules.pop("lanes", None)
                            if lanes is not None:
                                sys.modules["lanes"] = lanes
                    self.assertEqual(code, 1, "%s: %s" % (label, err))
                    self.assertIn("a command registrar replaced code a command runs by", err)
                    self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [], "nothing was cleared")
                    self.assertIn("a command registrar replaced code a command runs by", err)
                    self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [], "nothing was cleared")
            with self.subTest("the probes of the lint, run for real: each stops the command and the real score source is unchanged"):
                self.assertEqual(sb.impact_rating_entries, original_entries)
            with self.subTest("a default of a function the clear runs by, and a method of a class it uses"):
                import inspect
                defaults_of = next(n for n in daemon_names if getattr(sb, n).__defaults__)   # a function of the closure with defaults
                klass = next(v for n, v in sorted(vars(sb).items()) if inspect.isclass(v) and v.__module__ == sb.__name__ and any(inspect.isfunction(m) for m in vars(v).values()))
                method = next(n for n, m in vars(klass).items() if inspect.isfunction(m))
                def tweak_default(sub):
                    fn = getattr(sb, defaults_of)
                    fn.__defaults__ = tuple(reversed(fn.__defaults__)) if len(fn.__defaults__) > 1 else (object(),)
                def tweak_method(sub):
                    original = getattr(klass, method)
                    setattr(klass, method, lambda *a, **k: original(*a, **k))
                for label, registrar in (("a default", tweak_default), ("a method", tweak_method)):
                    fn = getattr(sb, defaults_of)
                    saved, original_method = fn.__defaults__, vars(klass)[method]
                    patcher = mock.patch.object(sb, "add_zzz_parsers", registrar, create=True)
                    patcher.start()
                    try:
                        code, out, err = self.under()
                    finally:
                        patcher.stop()
                        fn.__defaults__ = saved
                        setattr(klass, method, original_method)
                    self.assertEqual(code, 1, "%s: %s" % (label, err))
            with self.subTest("an action that is neither the clear nor the record"):
                patcher = mock.patch.object(sb, "add_zzz_parsers", self.reassign_registrar("dispatch"), create=True)
                patcher.start()
                original = sb.dispatch
                try:
                    code, out, err = self.run_as("cos", "version")
                finally:
                    patcher.stop()
                    sb.dispatch = original
                self.assertEqual(code, 1, err)

        LEDGER_FORGERY = ("ledger = sys.modules[__name__].plan_ledger", "ledger.read_ledger = F")   # M1 of the review of #935: the clear's score source (`plan_ledger.read_ledger`) replaced

        def registrar_probes(self):
            """(label, registrar body) for every shape that reaches another module from a registrar: the plain one, the six shapes that evaded the first ban list, and the neighbours. One table, used by the lint test and the runtime tests."""
            return {
                "the plain assignment": "sys.modules[__name__].plan_ledger.read_ledger = F",
                "an alias, then an assignment": "ledger = sys.modules[__name__].plan_ledger\n    ledger.read_ledger = F",
                "a loop target": "for sys.modules[__name__].plan_ledger.read_ledger in (F,):\n        pass",
                "a with target": "with contextlib.nullcontext(F) as sys.modules[__name__].plan_ledger.read_ledger:\n        pass",
                "a comprehension target": "[0 for sys.modules[__name__].plan_ledger.read_ledger in (F,)]",
                "a __dict__ update": "sys.modules[__name__].plan_ledger.__dict__.update(read_ledger=F)",
                "object.__setattr__": "object.__setattr__(sys.modules[__name__].plan_ledger, 'read_ledger', F)",
                "a __setattr__ call": "sys.modules[__name__].plan_ledger.__setattr__('read_ledger', F)",
                "setattr": "setattr(sys.modules[__name__].plan_ledger, 'read_ledger', F)",
                "a globals() write": "globals()['cmd_impact_clear'] = F",
                "a module name only": "m = plan_ledger\n    m.read_ledger = F",
                "a global statement": "global cmd_impact_clear\n    cmd_impact_clear = F",
                "a delete": "del builtins.print",
                "an augmented assignment": "sys.path += ['x']",
                "a tuple target": "a, sys.modules['x'] = 1, 2",
                "a type() reach": "type(sub).__setattr__(sub, 'x', F)",
            }

        def test_the_registrar_lint_is_an_allow_list_and_finds_every_shape_that_reaches_another_module(self):
            import ast
            for label, body in self.registrar_probes().items():
                with self.subTest(label):
                    found = self.registrar_findings(ast.parse("def add_zzz_parsers(sub):\n    " + body + "\n"))
                    self.assertTrue(found.get("add_zzz_parsers"), "the lint finds: %s" % body)
            clean = "def add_zzz_parsers(sub):\n    thing = sub.add_parser('zzz', help=argparse.SUPPRESS)\n    thing.add_argument('--x', type=int, default=argparse.SUPPRESS)\n    for flag in ('a',):\n        thing.add_argument('--' + flag)\n    group = thing.add_mutually_exclusive_group()\n    group.add_argument('--y', help='%d' % REPORT_MAX)\n"
            self.assertEqual(self.registrar_findings(ast.parse(clean)), {}, "a registrar that only sets up its own parser is clean")
            self.assertTrue(self.registrar_findings(ast.parse("def add_zzz_parsers(sub):\n    thing = sub.add_parser('zzz')\n    thing.usage = 'u'\n")), "even an attribute set on its own object is outside the vocabulary")

        def hostile_registrars_by_probe(self, calls):
            """REAL registrars, one for each probe of `registrar_probes` and the marker one: each first appends to `calls`, then does what its probe says. The direct route must never call any of them."""
            import contextlib
            out = {"a marker-writing registrar": "pass"}
            out.update(self.registrar_probes())
            out["the forged plan_ledger.read_ledger"] = "sys.modules[__name__].plan_ledger.read_ledger = F"
            registrars = {}
            for label, body in out.items():
                namespace = {"sys": sys, "__name__": sb.__name__, "contextlib": contextlib, "F": lambda *a, **k: ("FORGED", None), "builtins": __import__("builtins"), "plan_ledger": sb.plan_ledger, "calls": calls}
                sys.modules.setdefault(sb.__name__, sb)
                exec("def registrar(sub):\n    calls.append(1)\n    " + body + "\n", namespace)
                registrars[label] = namespace["registrar"]
            return registrars

        def test_a_hostile_registrar_is_never_called_when_the_record_runs_one_case_for_each_probe(self):
            """#924 rev 4: `review record` runs from the private parser: no registrar but its own is called, so none of the probes ever runs and the record is the clean one."""
            self.record_pr_setup()
            clean_code, clean_err, clean = self.record_run(self.EXPLICIT)
            self.assertEqual(clean_code, 0, clean_err)
            calls = []
            for label, registrar in self.hostile_registrars_by_probe(calls).items():
                with self.subTest(label):
                    code, err, entry = self.record_run(self.EXPLICIT, [("add_zzz_parsers", registrar)])
                    self.assertEqual((code, entry, calls), (0, clean, []), err)

        RAW_SPELLINGS = (("the exact tokens", []), ("an abbreviated subcommand", ["cle"]), ("a double dash after the command", ["--"]), ("an unknown flag", ["--zz"]), ("a help flag first", ["-h"]),
                         ("a capitalised subcommand", ["Clear"]))

        def traced_daemon_definitions(self, *argv):
            """The module-level definitions of the daemon file that RUN (a real call, observed by the interpreter) while the real `main` runs `argv`: (names, the run's exit code)."""
            import ast
            path = os.path.realpath(sb.__file__) if hasattr(sb, "__file__") else None
            defs = {(n.name, min([n.lineno] + [d.lineno for d in n.decorator_list])) for n in ast.parse(self.real_source()).body if isinstance(n, ast.FunctionDef)}   # name AND line: a method of the same name is not the definition
            seen = set()
            def profile(frame, event, arg):
                code = frame.f_code
                if event == "call" and (code.co_name, code.co_firstlineno) in defs and os.path.realpath(code.co_filename) == path:
                    seen.add(code.co_name)
            sys.setprofile(profile)
            try:
                code, out, err = self.run_as(*argv)
            finally:
                sys.setprofile(None)
            return seen, code, err

        def test_the_derived_closure_is_a_superset_of_the_run_time_trace_of_the_clear_and_the_record(self):
            """The contract (the Chief of Staff's ruling): the closure DERIVED from the code is never smaller than what the clear and the record really run. The real `main` runs `impact clear` and `review record`, the interpreter reports every
            daemon-file definition that was called, and each must be in the derived closure of the matrix's roots. A mutant that drops a traced definition from the closure fails this."""
            import ast
            rule = sb.impact_floor.load_matrix()["owner_hold"]["delegated_clear"]
            module = sb.load_module("delegated_clear")
            tree = ast.parse(self.real_source())
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            cleared, code, err = self.traced_daemon_definitions("cos", "impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity", "--brief-digest", self.FIXTURE_BRIEF["digest"], "--brief-text", self.FIXTURE_BRIEF["text"])
            self.assertEqual(code, 0, err)
            self.record_pr_setup()
            self.world.add_agent("rev", fleet={"role": "reviewer"}) if "rev" not in self.world.sessions else None
            recorded, code, err = self.traced_daemon_definitions("rev", "review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT)
            self.assertEqual(code, 0, err)
            traced = cleared | recorded
            self.assertTrue({"main", "cmd_impact", "cmd_review", "direct_command", "private_parser"} <= traced, sorted(traced))
            derived = lambda names: set(module.closure(tree, names, rule["dynamic_actions"])[0])
            closure = derived(module.daemon_roots(rule))
            self.assertEqual(sorted(traced - closure), [], "the derived closure must contain every definition the clear and the record really run")

        def test_no_spelling_of_the_clear_or_the_record_runs_in_a_process_where_a_registrar_ran(self):
            """#924 rev 4, the spelling table: for each spelling of the two commands (derived by running the real full parser over it), the outcome is a direct run with no registrar called, a usage error, or the refusal, never the command after a registrar."""
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            calls = []
            marker = self.hostile_registrars_by_probe(calls)["a marker-writing registrar"]
            base = {"impact": ["impact", "{}", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity"], "review": ["review", "{}", self.PR, *self.RATING_FLAGS, *self.EXPLICIT]}
            word = {"impact": "clear", "review": "record"}
            outcomes = {}
            for command in ("impact", "review"):
                for label, extra in self.RAW_SPELLINGS:
                    with self.subTest(command=command, spelling=label):
                        argv = [a.format(word[command]) for a in base[command]]
                        if label == "an abbreviated subcommand":
                            argv[1] = argv[1][:3]
                        elif label == "a capitalised subcommand":
                            argv[1] = argv[1].capitalize()
                        elif label == "a help flag first":
                            argv = ["-h"] + argv
                        elif label == "a double dash after the command":
                            argv = argv[:2] + ["--"] + argv[2:]
                        elif label == "an unknown flag":
                            argv = argv + ["--zz"]
                        before_writes, before_calls = len(self.writes), len(calls)
                        before_records = len([e for e in sb.plan_ledger.read_ledger(self.world.state)[0] if e["kind"] == "review_record"])
                        patcher = mock.patch.object(sb, "add_zzz_parsers", marker, create=True)
                        patcher.start()
                        try:
                            code, out, err = self.run_as("cos", *argv)
                        finally:
                            patcher.stop()
                        ran_registrar = len(calls) > before_calls
                        wrote = len([w for w in self.writes[before_writes:] if w[0] in ("DELETE", "POST", "PATCH", "PUT")]) > 0 or len([e for e in sb.plan_ledger.read_ledger(self.world.state)[0] if e["kind"] == "review_record"]) > before_records
                        if sb.direct_command(tuple(argv)):   # a direct run: no registrar is called
                            outcome = "direct"
                            self.assertFalse(ran_registrar, "the direct route calls no registrar")
                        elif code == 1:   # the refusal
                            outcome = "refused"
                            self.assertIn("a command registrar ran in this process", err)
                            self.assertFalse(wrote, "the refusal writes nothing")
                        else:   # a usage error (2) or the help of the parser (0, only for -h): nothing is written
                            outcome = "usage"
                            self.assertTrue(code == 2 or (code == 0 and "-h" in argv), "%r %r" % (code, err))
                            self.assertFalse(wrote, "a usage error or a help text writes nothing")
                        outcomes[(command, label)] = (code, ran_registrar, outcome)
                        self.assertTrue(outcome in ("direct", "refused", "usage"))
            self.assertEqual(outcomes[("impact", "the exact tokens")][2:], ("direct",))
            self.assertTrue({o[2] for o in outcomes.values()} >= {"direct", "usage"}, "the table reaches a direct run and a usage error")

        def test_with_the_pre_dispatch_off_the_clear_and_the_record_refuse_after_a_registrar_ran_and_a_registrar_cannot_reset_the_flag(self):
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            self.no_direct_route()
            code, out, err = self.under()
            self.assertEqual(code, 1, err)
            self.assertIn("a command registrar ran in this process", err)
            self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [])
            self.record_pr_setup()
            code, err, entry = self.record_run(self.EXPLICIT)
            self.assertEqual((code, entry), (1, None), err)
            self.assertIn("a command registrar ran in this process", err)
            def reset(sub):
                sb.REGISTRARS_RAN = False   # a module attribute nothing reads any more: the guard does not rest on it
            patcher = mock.patch.object(sb, "add_zzz_parsers", reset, create=True)
            patcher.start()
            try:
                code, out, err = self.under()
            finally:
                patcher.stop()
            self.assertEqual(code, 1, err)
            self.assertIn("a command registrar ran in this process", err)   # resetting a module attribute changes nothing: the guard does not rest on it
            self.assertFalse(hasattr(sb, "_WINDOW"), "main alone holds the window")

        def test_a_registrar_that_calls_the_clear_the_clears_core_or_the_record_itself_is_refused_at_the_point_of_effect(self):
            """#924, the review's probe: a registrar (here one standing in for `add_core_parsers`) builds the namespace of `impact clear` and CALLS the handlers itself during `main(["version"])`. Each must refuse (the flag is True while registrars run), write nothing, and the same calls after
            the registrars (the daemon's own chat clear runs there) are not refused by the flag."""
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            clear = sb.private_parser().parse_known_args(["impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity"])[0]
            record = sb.private_parser().parse_known_args(["review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT])[0]
            seen = {}
            def registrar(sub):
                with captured() as (o, e):
                    seen["cmd_impact_clear"] = (sb.cmd_impact_clear(clear, self.route, self.world.clock.now), e.getvalue())
                    seen["cmd_impact"] = sb.cmd_impact(clear, self.route, self.world.clock.now)
                    seen["cmd_review"] = sb.cmd_review(record, self.route, self.world.clock.now)
                try:
                    sb.impact_clear_core(None, self.route, "acme/app", 9, "cos", "a reason long enough")
                    seen["impact_clear_core"] = "ran"
                except RuntimeError as err:
                    seen["impact_clear_core"] = str(err)
                seen["window"] = sb.EFFECT_CHECK
            patcher = mock.patch.object(sb, "add_zzz_parsers", registrar, create=True)
            patcher.start()
            try:
                with captured() as (o, e):
                    code = sb.main(["version"], runner=self.route, now=self.world.clock.now)
            finally:
                patcher.stop()
            self.assertEqual(code, 1, "main fails closed after a registrar called an effect")
            self.assertIn("called the clear or the record itself", e.getvalue())
            self.assertEqual((seen["cmd_impact_clear"][0], seen["cmd_impact"], seen["cmd_review"]), (1, 1, 1))
            self.assertIn("a command registrar ran in this process", seen["cmd_impact_clear"][1])
            self.assertIn("a command registrar ran in this process", seen["impact_clear_core"])
            self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [], "nothing was cleared")
            self.assertFalse(sb.EFFECT_CHECK(), "the window is closed again after main")

        def guard_readers(self, daemon_source, hold_source, name):
            """The functions of the daemon file and of impact_hold.py that REFUSE on the guard `name`, read as a FREE variable (not a parameter of their own): an `if` of the function itself (not of a function nested in it) whose test reads it and whose body returns or raises. Derived by walking the code."""
            import ast
            def own(node):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                        continue
                    yield child
                    yield from own(child)
            found = set()
            for source in (daemon_source, hold_source):
                for node in ast.walk(ast.parse(source)):
                    if not isinstance(node, ast.FunctionDef) or name in {x.arg for x in ast.walk(node.args) if isinstance(x, ast.arg)} or any(isinstance(x, ast.Name) and x.id == name and isinstance(x.ctx, ast.Store) for x in ast.walk(node)):
                        continue
                    for x in own(node):
                        if isinstance(x, ast.If) and any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(x.test)) and any(isinstance(n, (ast.Return, ast.Raise)) for b in x.body for n in ast.walk(b)):
                            found.add(node.name)
            return found

        def test_no_guard_the_effects_read_is_a_module_global_main_writes(self):
            """Derived (the review of c77a44d4): `main` writes no module-level name (no `global` statement), and the refusing functions read the latch, a closure, not a flag. A build that brings back a module flag `main` sets is caught."""
            import ast, re as regex
            daemon = self.real_source()
            main = next(n for n in ast.parse(daemon).body if isinstance(n, ast.FunctionDef) and n.name == "main")
            self.assertEqual({name for g in ast.walk(main) if isinstance(g, ast.Global) for name in g.names}, set(), "main holds no global a registrar can write around")
            self.assertNotIn("REGISTRARS_RAN", daemon + (BIN / "impact_hold.py").read_text())
            readers = self.guard_readers(daemon, (BIN / "impact_hold.py").read_text(), "check")   # #924 round 4: the refusal is the guard's first statement, on the closure cell `check`, in `guarded_effect` (the daemon's and the module's)
            self.assertEqual(readers, {"guarded_effect"}, sorted(readers))
            for tree, names, guard in ((ast.parse(daemon), ("cmd_impact", "cmd_review"), "effect_guard"), (ast.parse((BIN / "impact_hold.py").read_text()), ("cmd_impact_clear", "impact_clear_core"), "_unbound")):
                for name in names:   # every effect is built by the guard
                    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
                    self.assertTrue(any(getattr(d.func, "id", "") == guard for d in node.decorator_list if isinstance(d, ast.Call)), name)
            reset_mutant = regex.sub(r"\n                if when\(arguments\) and check\(\):[^\n]*\n(?:                    [^\n]*\n)+", "\n", daemon)
            self.assertNotEqual(reset_mutant, daemon)
            self.assertNotIn("guarded_effect", self.guard_readers(reset_mutant, "", "check"), "with the read deleted no effect has a refusal: this test sees it")

        @staticmethod
        def guard_layer(effect):
            """the layer of an effect (`__wrapped__` chain, `recording` outside it) that holds the latch check in its closure (#924 round 4): the guard's `guarded_effect`"""
            while effect is not None:
                if "check" in effect.__code__.co_freevars:
                    return effect
                effect = getattr(effect, "__wrapped__", None)
            raise AssertionError("no guard layer")

        @classmethod
        def check_cell(cls, effect):
            layer = cls.guard_layer(effect)
            return layer.__closure__[layer.__code__.co_freevars.index("check")]

        @classmethod
        def rewire_check(cls, effect, replacement):
            """a MUTANT (a patcher): the effect's check cell holds `replacement` while it is started (the forgery the docs state as the limit, used here to build the mutants)"""
            cell = cls.check_cell(effect)
            saved = cell.cell_contents
            class Patch:
                def start(self_inner):
                    cell.cell_contents = replacement
                def stop(self_inner):
                    cell.cell_contents = saved
            return Patch()

        def reset_call_restore(self, flag_based=False):
            """A hostile registrar sets the module flag False, calls each effect, then sets it back to True. Returns what each call did."""
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            clear = sb.private_parser().parse_known_args(["impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity"])[0]
            record = sb.private_parser().parse_known_args(["review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT])[0]
            seen = {}
            def registrar(sub):
                sb.REGISTRARS_RAN = False
                try:
                    with captured() as (o, e):
                        seen["clear"] = (sb.cmd_impact_clear(clear, self.route, self.world.clock.now), e.getvalue())
                        seen["record"] = (sb.cmd_review(record, self.route, self.world.clock.now), e.getvalue())
                    try:
                        sb.impact_clear_core(None, self.route, "acme/app", 9, "cos", "a reason long enough")
                        seen["core"] = "ran"
                    except RuntimeError as err:
                        seen["core"] = str(err)
                    except AttributeError:   # past the guard, at `engine.audit` of the None engine (the review's probe)
                        seen["core"] = "ran"
                finally:
                    sb.REGISTRARS_RAN = True
            patchers = [mock.patch.object(sb, "add_zzz_parsers", registrar, create=True), mock.patch.object(sb, "REGISTRARS_RAN", True, create=True)]
            if flag_based:   # the MUTANT: a build that decides from the module global a registrar can write (the checks the effects hold, bound at definition, are replaced by one that reads it)
                for effect in (sb.cmd_impact, sb.cmd_review, sb.cmd_impact_clear, sb.impact_clear_core):
                    patchers.append(self.rewire_check(effect, lambda: sb.REGISTRARS_RAN))
            for patcher in patchers:
                patcher.start()
            try:
                with captured() as (o, e):
                    code = sb.main(["version"], runner=self.route, now=self.world.clock.now)
            finally:
                for patcher in reversed(patchers):
                    patcher.stop()
            return code, seen, e.getvalue()

        def test_a_registrar_that_resets_a_module_flag_calls_the_clear_and_the_record_and_restores_it_is_refused(self):
            code, seen, err = self.reset_call_restore()
            self.assertEqual((seen["clear"][0], seen["record"][0]), (1, 1), seen)
            self.assertIn("a command registrar ran in this process", seen["clear"][1])
            self.assertIn("a command registrar ran in this process", seen["core"])
            self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [])
            self.assertEqual(code, 1, "main itself fails closed when an effect was called while the registrars ran")
            self.assertIn("called the clear or the record itself", err)

        def test_the_mutant_that_decides_from_the_module_global_lets_the_reset_call_restore_through(self):
            """The mutant of the High (c77a44d4): the effects read a module flag. The same attack is NOT refused at the point of effect (the core is reached), which is what the previous test would catch."""
            code, seen, err = self.reset_call_restore(flag_based=True)
            self.assertEqual(seen["core"], "ran", "the flag-based build does not refuse: the attack works, so the test above discriminates")
            self.assertNotIn("a command registrar ran in this process", seen["clear"][1])

        EFFECTS = ("cmd_impact", "cmd_impact_clear", "cmd_review", "impact_clear_core")   # the functions that write: the clear (and its core) and the record

        def effect_globals(self, daemon_source=None, namespace=None):
            """The ATTACK SET, derived by ast: every name the effect functions read at call time from their module globals (a Load that no parameter, assignment, import, def or comprehension variable of the function binds, and that the daemon's namespace holds)."""
            import ast, builtins
            namespace = vars(sb) if namespace is None else namespace
            daemon, hold = ast.parse(daemon_source or self.real_source()), ast.parse((BIN / "impact_hold.py").read_text())
            functions = {n.name: n for tree in (daemon, hold) for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in self.EFFECTS and n.args.vararg is None}   # the built effect, not the unbound stand-in (`*args`)
            self.assertEqual(sorted(functions), sorted(self.EFFECTS))
            found = {}
            for name, node in functions.items():
                bound = {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
                for x in ast.walk(node):
                    if isinstance(x, ast.Name) and isinstance(x.ctx, (ast.Store, ast.Del)):
                        bound.add(x.id)
                    elif isinstance(x, (ast.FunctionDef, ast.ClassDef)) and x is not node:
                        bound.add(x.name)
                    elif isinstance(x, (ast.Import, ast.ImportFrom)):
                        bound |= {(a.asname or a.name).split(".")[0] for a in x.names}
                    elif isinstance(x, ast.ExceptHandler) and x.name:
                        bound.add(x.name)
                body = ast.Module(body=node.body + [ast.Expr(d) for d in node.decorator_list], type_ignores=[])   # the defaults are evaluated at definition, not at call time: the body reads a global when the effect runs, and so do the guard's refusal and condition (the decorator's lambdas)
                found[name] = {x.id for x in ast.walk(body) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id not in bound
                               and x.id not in vars(builtins) and x.id in namespace}
            return found

        def test_the_effects_read_no_latch_by_global_name_and_the_attack_set_is_derived(self):
            by_function = self.effect_globals()
            every = set().union(*by_function.values())
            self.assertNotIn("EFFECT_CHECK", every, "the check is bound at definition: no effect looks it up by global name")
            for name in self.EFFECTS:
                effect = getattr(sb, name)   # `cmd_review` is wrapped by `recording`: the check is the guard layer's closure cell
                self.assertTrue(callable(self.check_cell(effect).cell_contents), name)
                self.assertIsNone(inspect.unwrap(effect).__kwdefaults__, name)
                self.assertFalse(set(inspect.signature(effect).parameters) & {"_effect_check", "check", "effect_check"}, name)
            self.assertTrue({"rating_inputs", "gates_caller_kind", "build_engine"} <= every | {"rating_inputs", "gates_caller_kind", "build_engine"}, sorted(every)[:10])
            self.assertGreater(len(every), 5, sorted(every))
            mutant = self.real_source().replace("def cmd_impact(args, runner, now):\n    \"\"\"`impact floor|hold|clear`.\"\"\"\n", "def cmd_impact(args, runner, now):\n    \"\"\"`impact floor|hold|clear`.\"\"\"\n    zzz_unlisted_global()\n", 1)
            self.assertNotEqual(mutant, self.real_source())
            grown = self.effect_globals(mutant, dict(vars(sb), zzz_unlisted_global=len))
            self.assertIn("zzz_unlisted_global", grown["cmd_impact"], "an effect that starts reading a new module global is in the derived attack set (the parametrised test then runs it)")

        def attack(self, rebound):
            """A hostile registrar rebinds the module-level name `rebound` (to a lambda that gives the permissive answer), calls each effect, restores it. (what each did, the DELETE writes, main's code and its stderr)"""
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            clear = sb.private_parser().parse_known_args(["impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity"])[0]
            record = sb.private_parser().parse_known_args(["review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT])[0]
            seen = {}
            def registrar(sub):
                original = getattr(sb, rebound)
                setattr(sb, rebound, (lambda *a, **k: False) if rebound == "EFFECT_CHECK" else (lambda *a, **k: None))
                try:
                    for label, call in (("clear", lambda: sb.cmd_impact_clear(clear, self.route, self.world.clock.now)), ("impact", lambda: sb.cmd_impact(clear, self.route, self.world.clock.now)),
                                        ("record", lambda: sb.cmd_review(record, self.route, self.world.clock.now)), ("core", lambda: sb.impact_clear_core(None, self.route, "acme/app", 9, "cos", "a reason long enough"))):
                        try:
                            with captured() as (o, e):
                                seen[label] = call()
                        except Exception as err:   # an effect that fails closed (any refusal or error) did nothing
                            seen[label] = "error: %s" % type(err).__name__
                finally:
                    setattr(sb, rebound, original)
            patcher = mock.patch.object(sb, "add_zzz_parsers", registrar, create=True)
            patcher.start()
            try:
                with captured() as (o, e):
                    code = sb.main(["version"], runner=self.route, now=self.world.clock.now)
            finally:
                patcher.stop()
            return seen, [w for w in self.writes if w[0] == "DELETE"], code, e.getvalue()

        STUBS = {"cmd_impact": "impact", "cmd_impact_clear": "clear", "cmd_review": "record", "impact_clear_core": "core"}

        def attack_refused(self, seen, deletes, code, rebound=None):
            for label, result in seen.items():
                if self.STUBS.get(rebound) == label:   # the registrar replaced THIS effect by its own stub: the call reaches the stub (which writes nothing); the snapshot and main's refusal catch the replacement
                    continue
                self.assertNotIn(result, (0, "ran", None), "the %s was not refused: %r" % (label, seen))
            self.assertEqual(deletes, [], "nothing was cleared")
            self.assertEqual(code, 1, "main fails closed")

        def test_a_registrar_that_rebinds_any_global_name_an_effect_reads_calls_the_effects_and_restores_it_is_refused_for_every_name(self):
            """#924 round 2: the attack set is every module global an effect reads (derived by ast: the latch's own name included), and for each one the reset-call-restore attack is refused for the clear (`cmd_impact_clear`, `cmd_impact`, `impact_clear_core`) and the record (`cmd_review`)."""
            every = sorted(set().union(*self.effect_globals().values()) | {"EFFECT_CHECK"})
            self.assertIn("EFFECT_CHECK", every)
            for name in every:
                with self.subTest(rebound=name):
                    seen, deletes, code, err = self.attack(name)
                    self.attack_refused(seen, deletes, code, name)
                    self.assertIn("called the clear or the record itself", err)

        def test_the_mutant_that_looks_the_check_up_by_global_name_again_lets_the_rebinding_through(self):
            """The mutant of the review: every effect's check is a lookup of `sb.EFFECT_CHECK` at call time. The rebinding attack then gets past the guard of the clear, the record and the core (the discriminating half: the test above refuses all of them)."""
            with contextlib.ExitStack() as stack:
                for effect in self.EFFECTS:
                    patch = self.rewire_check(getattr(sb, effect), lambda: sb.EFFECT_CHECK())
                    patch.start()
                    stack.callback(patch.stop)
                seen, deletes, code, err = self.attack("EFFECT_CHECK")
            self.assertNotEqual(seen["clear"], 1, "the clear is past its guard: %r" % seen)
            self.assertNotEqual(seen["record"], 1, "the record is past its guard: %r" % seen)
            self.assertNotEqual(seen["core"], "error: RuntimeError", "the core is past its guard: %r" % seen)

        def test_what_a_registrar_can_reach_of_the_latch_is_stated_by_trying_it(self):
            """Not a sandbox: the latch is a closure, not a module attribute, but a registrar that reaches into it (the cell of the effect function, `main.__defaults__`) can forge it. This test TRIES it and pins the answer: LATCH FORGEABLE: yes, by these two routes; main's own copy of the count still catches a registrar that only calls an effect."""
            cell = next(c for c in sb.EFFECT_CHECK.__closure__ if isinstance(c.cell_contents, dict))
            self.assertTrue({"open", "calls"} <= set(cell.cell_contents), "the cell is reachable from the function")
            window = sb.main.__defaults__[-1]
            self.assertTrue(callable(window), "the window is reachable from main's defaults")
            saved = dict(cell.cell_contents)
            try:
                cell.cell_contents["open"] = True
                self.assertTrue(sb.EFFECT_CHECK(), "a registrar that writes the cell forges the answer")
            finally:
                cell.cell_contents.update(saved)

        def test_a_hostile_default_or_replaced_subparser_on_the_clear_under_the_real_build_parser_with_the_direct_route_off_is_refused(self):
            """The clear-side tests above run the direct route's parser; here `main` takes the NORMAL route (the pre-dispatch off) with the real `build_parser` wrapped by a hostile `set_defaults(reason=...)` and a replaced `impact` subparser: the clear is refused and nothing is written."""
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            self.no_direct_route()
            real = sb.build_parser
            def sets_reason():
                parser = real()
                (top,) = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
                top.choices["impact"].set_defaults(reason="a hostile reason that is long enough to pass")
                return parser
            def replaced_impact():
                parser = real()
                (top,) = [a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction"]
                fresh = argparse.ArgumentParser().add_subparsers(dest="action")
                sb.add_impact_parsers(fresh)
                top.choices["impact"] = fresh.choices["impact"]
                return parser
            for label, builder in (("set_defaults(reason)", sets_reason), ("replaced subparser", replaced_impact)):
                with self.subTest(label), mock.patch.object(sb, "build_parser", builder):
                    code, out, err = self.under()
                self.assertEqual(code, 1, err)
                self.assertIn("a command registrar ran in this process", err)
                self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [])

        def effect_calls(self):
            """{effect name: a function (override={param: value}) -> the call, every parameter passed BY NAME}: the real effects, called in the window of a registrar"""
            clear = sb.private_parser().parse_known_args(["impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity"])[0]
            record = sb.private_parser().parse_known_args(["review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT])[0]
            now = self.world.clock.now
            cmd = lambda name, args: (lambda **over: getattr(sb, name)(**dict({"args": args, "runner": self.route, "now": now}, **over)))
            core = lambda **over: sb.impact_clear_core(**dict({"engine": None, "runner": self.route, "repo": "acme/app", "number": 9, "who": "cos", "reason": "a reason long enough", "remove": None,
                                                               "head": None, "via": None, "verify": None, "under_order": None, "brief": None}, **over))
            return {"cmd_impact": cmd("cmd_impact", clear), "cmd_impact_clear": cmd("cmd_impact_clear", clear), "cmd_review": cmd("cmd_review", record), "impact_clear_core": core}

        def test_no_effect_takes_the_check_as_a_parameter(self):
            """#924 round 4, test 1: `_effect_check=` (and `check=`) on each of the four effects is a TypeError, at the call, before any statement runs"""
            calls = self.effect_calls()
            self.assertEqual(sorted(calls), sorted(self.EFFECTS))
            for name, call in calls.items():
                for keyword in ("_effect_check", "check", "EFFECT_CHECK"):
                    with self.subTest(effect=name, keyword=keyword), self.assertRaises(TypeError):
                        call(**{keyword: lambda: False})
            for name in ("cmd_impact", "cmd_impact_clear", "cmd_review", "impact_clear_core"):   # the signatures themselves, with no call
                names = set(inspect.signature(getattr(sb, name)).parameters)
                self.assertFalse(names & {"_effect_check", "check", "effect_check"}, name)

        def test_the_mutant_that_brings_the_keyword_back_is_not_a_typeerror(self):
            """MUTANT: the clear of a copy of impact_hold.py that takes `_effect_check` again accepts the keyword (the signature binds it): the real one does not (the discriminating half). Only signatures are bound: nothing is called."""
            source = (BIN / "impact_hold.py").read_text()
            old = "def cmd_impact_clear(args, runner, now):"
            self.assertEqual(source.count(old), 1)
            module = types.ModuleType("impact_hold_kwarg")
            exec(compile(source.replace(old, "def cmd_impact_clear(args, runner, now, *, _effect_check=None):"), "impact_hold_kwarg", "exec"), module.__dict__)
            fresh = dict(sb.__dict__)
            module.bind(fresh, sb.EFFECT_CHECK)
            inspect.signature(fresh["cmd_impact_clear"]).bind_partial(_effect_check=lambda: False)   # the mutant takes it
            with self.assertRaises(TypeError):
                inspect.signature(sb.cmd_impact_clear).bind_partial(_effect_check=lambda: False)

        def test_a_registrar_that_sets_the_check_permissive_binds_again_calls_the_new_effect_and_restores_is_refused(self):
            """#924 round 4, test 2 (the High (B)): reset the global, `bind` again (with and without the check), keep the effect `bind` returned, restore: bind refuses, nothing is replaced, and each effect refuses"""
            hold = sb.load_module("impact_hold")
            originals = {name: getattr(sb, name) for name in set(hold.MOVED) | set(self.EFFECTS)}
            def body(seen):
                real = sb.EFFECT_CHECK
                sb.EFFECT_CHECK = lambda: False
                try:
                    for label, call in (("bind(ns)", lambda: hold.bind(sb.__dict__)), ("bind(ns, permissive)", lambda: hold.bind(sb.__dict__, lambda: False)),
                                        ("bind(ns, global)", lambda: hold.bind(sb.__dict__, sb.EFFECT_CHECK)), ("bind(dict copy, global)", lambda: hold.bind(dict(sb.__dict__))), ("bind(new dict, permissive)", lambda: hold.bind(dict(sb.__dict__), lambda: False))):
                        seen[label] = self.outcome(call)
                    kept = {name: getattr(sb, name) for name in self.EFFECTS}
                finally:
                    sb.EFFECT_CHECK = real
                for name, call in self.effect_calls().items():
                    seen[name] = self.outcome(lambda: kept[name](**{p: v for p, v in self.effect_args(name).items()}))
                seen["kept"] = kept
            seen, deletes, code, err = self.hostile(body)
            kept = seen.pop("kept")
            self.assertEqual(seen["bind(ns)"], "error: TypeError", "bind takes no namespace-only call: the check has no other source")
            self.assertEqual(seen["bind(ns, permissive)"], "refused"); self.assertEqual(seen["bind(ns, global)"], "refused")
            self.assertEqual(seen["bind(dict copy, global)"], "error: TypeError")
            self.assertEqual(seen["bind(new dict, permissive)"], "ran", "the STATED LIMIT: a bind into a NEW namespace builds effects there (the daemon's are untouched, checked below); a registrar can equally write a clear of its own")
            for name in self.EFFECTS:
                self.assertEqual(seen[name], "refused", seen)
                self.assertIs(kept[name], originals[name], "bind replaced nothing: the effect is the same object")
                self.assertIs(getattr(sb, name), originals[name])
            self.assertEqual(deletes, [])
            self.assertEqual(code, 1)

        def effect_args(self, name):
            """the arguments of one real call of the effect `name`, by parameter name"""
            clear = sb.private_parser().parse_known_args(["impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity"])[0]
            record = sb.private_parser().parse_known_args(["review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT])[0]
            if name == "impact_clear_core":
                return {"engine": None, "runner": self.route, "repo": "acme/app", "number": 9, "who": "cos", "reason": "a reason long enough", "remove": None, "head": None, "via": None, "verify": None, "under_order": None, "brief": None}
            return {"args": record if name == "cmd_review" else clear, "runner": self.route, "now": self.world.clock.now}

        def test_the_mutant_that_reads_the_namespace_at_bind_time_is_refused_by_the_reset_bind_test(self):
            """MUTANT: a `bind` that takes the check from `namespace['EFFECT_CHECK']` at call time (the old shape) hands the effects the permissive global: the effect `bind` made is NOT refused, so the test above discriminates"""
            source = (BIN / "impact_hold.py").read_text()
            old = "        guard, here = make_guard(check), globals()"
            self.assertEqual(source.count(old), 1)
            mutant = source.replace(old, "        guard, here = make_guard(namespace['EFFECT_CHECK']), globals()").replace("        if not callable(check):", "        if False:")
            module = types.ModuleType("impact_hold_namespace_reader")
            exec(compile(mutant, "impact_hold_namespace_reader", "exec"), module.__dict__)
            fresh = dict(sb.__dict__, EFFECT_CHECK=lambda: False)
            module.bind(fresh, None)
            self.assertEqual(self.outcome(lambda: fresh["cmd_impact_clear"](**self.effect_args("cmd_impact_clear"))) != "refused", True, "the namespace-reading bind hands the permissive check to the effect")

        # the callable surface: every public PARAMETER of the effects and every function that builds or installs an effect, derived by ast (a new one without a case fails the completeness test)
        SURFACE = {("cmd_impact", "args"), ("cmd_impact", "runner"), ("cmd_impact", "now"), ("cmd_review", "args"), ("cmd_review", "runner"), ("cmd_review", "now"),
                   ("cmd_impact_clear", "args"), ("cmd_impact_clear", "runner"), ("cmd_impact_clear", "now"),
                   *(("impact_clear_core", p) for p in ("engine", "runner", "repo", "number", "who", "reason", "remove", "head", "via", "verify", "under_order", "brief")),
                   ("bind", "namespace"), ("bind", "check"), ("_make_bind", "make_guard"), ("_make_guard", "check"), ("make_effect_guard", "check"), ("guard", "when"), ("guard", "refuse"),
                   ("decorate", "effect"), ("guarded_effect", "args"), ("guarded_effect", "kwargs"), ("rebuilt", "function"), ("rebuilt", "namespace"), ("make_effect_latch", None)}

        def derived_surface(self, daemon_source=None):
            import ast
            found = set()
            for is_hold, source in ((False, daemon_source or self.real_source()), (True, (BIN / "impact_hold.py").read_text())):
                for node in ast.walk(ast.parse(source)):
                    if not isinstance(node, ast.FunctionDef):
                        continue
                    builds = any(isinstance(x, ast.Call) and getattr(x.func, "attr", getattr(x.func, "id", "")) == "FunctionType" for x in ast.walk(node))   # installs a function into a namespace
                    factory = any(isinstance(x, ast.FunctionDef) and x.name == "guarded_effect" for x in ast.walk(node)) or node.name == "guarded_effect"   # the guard's wrapper, or whatever builds it: builds an effect around a check
                    if node.name in self.EFFECTS or factory or node.name == "make_effect_latch" or (is_hold and (node.name == "bind" or builds)):
                        a = node.args
                        params = [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs] + [x.arg for x in (a.vararg, a.kwarg) if x]
                        found |= {(node.name, p) for p in params} or {(node.name, None)}
            return found

        def test_the_callable_surface_of_the_effects_and_of_what_builds_them_is_derived_and_covered(self):
            derived = self.derived_surface()
            self.assertEqual(derived, self.SURFACE, "a parameter or a builder that is not in SURFACE has no case: add it with its test (or the change adds a way to hand an effect its check)")
            mutant = self.real_source().replace("def cmd_impact(args, runner, now):", "def cmd_impact(args, runner, now, _effect_check=None):", 1)
            self.assertNotEqual(mutant, self.real_source())
            self.assertIn(("cmd_impact", "_effect_check"), self.derived_surface(mutant), "re-adding the keyword is a new surface entry: the completeness test fails on it")
            reader = self.real_source().replace("def make_effect_guard(check):", "def make_effect_guard(check, namespace=None):", 1)
            self.assertIn(("make_effect_guard", "namespace"), self.derived_surface(reader))

        def test_every_parameter_of_every_effect_is_refused_in_the_window_of_a_registrar_even_as_a_permissive_value(self):
            """one case per (effect, parameter) of the derived surface: that parameter is given a permissive callable (everything else real); the effect is refused or fails closed, and nothing is written"""
            cases = sorted((name, p) for name, p in self.derived_surface() if name in self.EFFECTS)
            self.assertGreaterEqual(len(cases), 17)
            def body(seen):
                for name, p in cases:
                    args = dict(self.effect_args(name), **{p: (lambda *a, **k: False)})
                    seen[(name, p)] = self.outcome(lambda: getattr(sb, name)(**args))
            seen, deletes, code, err = self.hostile(body)
            for case in cases:
                self.assertNotEqual(seen[case], "ran", case)
            self.assertEqual(deletes, [])

        def test_the_builders_of_an_effect_are_not_reachable_by_name(self):
            """`make_effect_guard` (the entry), `_make_guard` and `_make_bind` (impact_hold) are deleted after their one call: no module name builds a guard around a check of the caller's choosing"""
            hold = sb.load_module("impact_hold")
            for name in ("make_effect_guard", "_make_guard", "_make_bind", "guard", "decorate", "guarded_effect", "rebuilt"):
                self.assertFalse(hasattr(sb, name) or hasattr(hold, name), name)

        PUBLIC_WITH_KWARGS = {("entry", "review_refusal_audit"): "audit data", ("entry", "call_command"): "filters by the handler's own signature", ("entry", "update_daemon_record"): "daemon.json fields",
                              ("entry", "add_repeat_reminder"): "reminder data"}

        def test_every_public_callable_of_the_entry_and_of_impact_hold_refuses_the_hostile_registrar(self):
            """#924 round 4, the hostile registrar over ALL public callables, derived at test time (every callable of the two modules that does not start with an underscore, a class excluded). A callable that takes no keyword
            for a check is a TypeError at the call (the signature binds nothing); one that takes `check` is `bind` alone, which a registrar's call refuses; one that takes `**kwargs` is in a table and, where it forwards to
            a handler, is run for real. The list is derived: a new public callable of either shape without a case fails here."""
            hold = sb.load_module("impact_hold")
            checkish = {"check", "_effect_check", "effect_check", "EFFECT_CHECK"}
            accepts, kwargs_taking, closed = [], [], 0
            for label, module in (("entry", sb), ("impact_hold", hold)):
                for name, value in sorted(vars(module).items()):
                    if name.startswith("_") or not callable(value) or isinstance(value, type):
                        continue
                    try:
                        signature = inspect.signature(value)
                    except (TypeError, ValueError):
                        continue
                    kinds = {p.kind for p in signature.parameters.values()}
                    if set(signature.parameters) & checkish:
                        accepts.append((label, name))
                    elif inspect.Parameter.VAR_KEYWORD in kinds:
                        kwargs_taking.append((label, name))
                    else:
                        closed += 1
                        for keyword in sorted(checkish):
                            with self.assertRaises(TypeError, msg="%s.%s takes %s" % (label, name, keyword)):
                                signature.bind_partial(**{keyword: lambda: False})
            self.assertGreater(closed, 1000, "the derivation saw the public callables of the entry")
            self.assertEqual(accepts, [("impact_hold", "bind")], "the only public callable that takes a check is bind")
            self.assertEqual(sorted(kwargs_taking), sorted(self.PUBLIC_WITH_KWARGS), "a public callable with **kwargs that is not in the table has no case")
            def body(seen):
                seen["bind"] = [self.outcome(lambda: hold.bind(sb.__dict__, lambda: False)), self.outcome(lambda: hold.bind(sb.__dict__, sb.EFFECT_CHECK))]
                for name in ("impact_clear_core", "cmd_impact_clear"):   # the stand-ins of the module (the unbound copies)
                    seen["unbound " + name] = self.outcome(lambda: getattr(hold, name)(_effect_check=lambda: False))
                for name, call in self.effect_calls().items():   # call_command filters a keyword the handler does not name: the permissive check never reaches it
                    seen["call_command " + name] = self.outcome(lambda: sb.call_command(getattr(sb, name), _effect_check=lambda: False, **self.effect_args(name)))
            seen, deletes, code, err = self.hostile(body)
            self.assertEqual(seen["bind"], ["refused", "refused"])
            for key, result in seen.items():
                if key != "bind":
                    self.assertNotEqual(result, "ran", key)
            self.assertEqual(deletes, [])

        def test_the_handlers_themselves_refuse_while_a_registrar_ran(self):
            """The point of effect (#924): `cmd_impact` (clear) and `cmd_review` (record) read the process flag, so a route that reached them in a process where a registrar ran stops there, whatever main did."""
            self.delegate(); self.floor_pr(path="migrations/0002.sql"); self.files_of_pr("migrations/0002.sql"); self.passes()
            clear = sb.private_parser().parse_known_args(["impact", "clear", self.PR, "--under-order", "17", "--reason", "the review confirms nothing touches credentials nor identity"])[0]
            record = sb.private_parser().parse_known_args(["review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT])[0]
            for label, handler, args in (("impact clear", sb.cmd_impact, clear), ("review record", sb.cmd_review, record)):
                with self.subTest(label), captured() as (o, e):
                    # the window is opened by main alone: opened here through main's own handle
                    window = sb.main.__defaults__[-1]; window(True)
                    self.addCleanup(window, False)
                    self.assertEqual(handler(args, self.route, self.world.clock.now), 1)
                    self.assertIn("a command registrar ran in this process", e.getvalue())
            self.assertEqual([w for w in self.writes if w[0] == "DELETE"], [])

        def test_every_other_command_still_runs_the_registrars_as_today(self):
            calls = []
            marker = self.hostile_registrars_by_probe(calls)["a marker-writing registrar"]
            patcher = mock.patch.object(sb, "add_zzz_parsers", marker, create=True)
            patcher.start()
            try:
                with captured() as (o, e):
                    code = sb.main(["version"], runner=self.route, now=self.world.clock.now)
                out, err = o.getvalue(), e.getvalue()
            finally:
                patcher.stop()
            self.assertEqual((code, calls), (0, [1]), err)
            self.assertIn(sb.VERSION, out)

        def test_the_private_parser_reads_the_two_commands_exactly_as_the_full_parser_does(self):
            lines = [["impact", "clear", self.PR, "--under-order", "17", "--reason", "a long enough reason"], ["impact", "clear", self.PR, "--reason", "a long enough reason", "--revoke"],
                     ["review", "record", self.PR, *self.RATING_FLAGS, *self.EXPLICIT], ["review", "record", self.PR, "--reviewer", "code", "--verdict", "approve", "--blocks", "2", "--notes", "1"],
                     ["review", "record", self.PR, "--reviewer", "impact", "--block", "-"]]
            for line in lines:
                with self.subTest(line=line[:3] + line[3:5]):
                    full = vars(sb.build_parser().parse_known_args(line)[0])
                    private = vars(sb.private_parser().parse_known_args(line)[0])
                    self.assertEqual(private, full)
            self.assertEqual(sorted(sb.private_parser().commands), ["impact", "review"])

    for _name in dir(TestImpactHold):   # the inherited tests run on TestImpactHold
        if _name.startswith("test") and _name not in vars(TestImpactHold924):
            setattr(TestImpactHold924, _name, None)


    for _index, (_label, _mutator) in enumerate(hostile_parser_cases()):
        setattr(TestImpactHold924, "test_the_clear_reads_what_the_command_line_and_the_ledger_say_under_a_hostile_build_parser_%d" % _index, _hostile_clear_test(_index))

    for _label in ("a marker-writing registrar", "the forged plan_ledger.read_ledger", "the plain assignment", "an alias, then an assignment", "a loop target", "a with target", "a comprehension target", "a __dict__ update", "object.__setattr__",
                   "a __setattr__ call", "setattr", "a globals() write", "a module name only", "a global statement", "a delete", "an augmented assignment", "a tuple target", "a type() reach"):
        setattr(TestImpactHold924, "test_a_hostile_registrar_is_never_called_when_the_clear_runs_%s" % re.sub(r"\W+", "_", _label).strip("_"), _hostile_registrar_clear_test(_label))

    return TestPR8Source924, TestImpactHold924
