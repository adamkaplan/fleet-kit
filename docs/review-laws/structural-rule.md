# Deciding from text: ask for the class in round one

When a function decides from text (an error message, a substring, a pattern, a name list), the first-round finding asks for an allow-list, a structured field or exact equality, and for a completeness test derived from the code, with one mutant per member, so the class is closed in one round and not found one instance at a time.

Example (neutral names). A function in `acme/widget-api` decides whether a request is a read or a write by looking for the word `list` in the command line, so `list-and-delete` is taken for a read. The first-round finding does not name that one command. It asks for a table of the allowed commands, each with its kind, so that the decision is exact equality against the table, and for a test that walks the table and fails when a command is missing from it, with one mutant per row (remove the row, flip its kind) so that every command is proved, not the one that was noticed.
