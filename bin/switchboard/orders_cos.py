# ---- orders --cos

def cmd_orders_cos(args):
    try:
        found = read_cos_orders(load_config())
    except CosOrdersError as err:
        sys.stderr.write("%s: %s\n" % (PROG, _one_line(str(err), 400)))
        return EXIT_ERROR
    if not found["exists"]:
        print("none: there is no standing-orders file at %s (`%s install` makes a template; edit it in an editor)"
              % (found["path"], PROG))
        return EXIT_OK
    print("The Chief of Staff's standing orders: %s (%d bytes, modified %s)" % (
        found["path"], found["size"], iso(found["mtime"])))
    print()
    print(found["raw"].rstrip("\n"))
    return EXIT_OK
