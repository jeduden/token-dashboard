"""Table-driven tests for token_dashboard.bash_keys (pure Bash key extraction)."""
import unittest

from token_dashboard.bash_keys import bash_key

# (command, expected command, expected command2, expected pipeline)
CASES = [
    # plain
    ("git status", "git", "git status", "git status"),
    ("ls", "ls", "ls", "ls"),
    ("npm test -- --watch", "npm", "npm test", "npm test"),
    # cd prefixes are stripped only when another command follows
    ("cd /some/dir && git status", "git", "git status", "git status"),
    ("cd /a; cd b && npm test", "npm", "npm test", "npm test"),
    ("cd '/path with space' && ls -la", "ls", "ls -la", "ls"),
    ("cd /tmp", "cd", "cd /tmp", "cd"),
    ("cd", "cd", "cd", "cd"),
    ("cd /a || echo nope", "cd", "cd /a", "cd || echo"),
    ("(cd x && make build)", "make", "make build", "make build"),
    ("export FOO=1 && pytest -q", "pytest", "pytest -q", "pytest"),
    # assignments and wrappers
    ("FOO=1 BAR=2 npm test", "npm", "npm test", "npm test"),
    ("sudo apt-get install x", "apt-get", "apt-get install", "apt-get install"),
    ("sudo -u bob git pull", "git", "git pull", "git pull"),
    ("env -i FOO=1 make all", "make", "make all", "make all"),
    ("time pytest tests", "pytest", "pytest tests", "pytest"),
    ("nohup python3 serve.py &", "python3", "python3 serve.py", "python3"),
    ("timeout 30 npm test", "npm", "npm test", "npm test"),
    ("timeout -s KILL 30 go test ./...", "go", "go test", "go test"),
    ("command -v git", "command", "command -v", "command"),
    ("exec node app.js", "node", "node app.js", "node"),
    ("nice -n 10 make", "make", "make", "make"),
    ("xargs grep foo", "xargs", "xargs grep", "xargs"),
    # shell -c recursion
    ("bash -c 'cd /x && git log'", "git", "git log", "git log"),
    ("sh -c \"npm run build\"", "npm", "npm run", "npm run"),
    ("zsh -lc 'FOO=1 make'", "make", "make", "make"),
    ("bash -c 'bash -c \"echo hi\"'", "echo", "echo hi", "echo"),
    ("bash script.sh", "bash", "bash script.sh", "bash"),
    ("bash -c 'echo unbalanced", "echo", "echo unbalanced", "echo"),
    # compound commands: head of the pipeline is the key
    ("git log | head", "git", "git log", "git log | head"),
    ("a && b", "a", "a", "a && b"),
    ("make test || echo failed", "make", "make test", "make test || echo"),
    ("ls; pwd", "ls", "ls", "ls ; pwd"),
    ("cd d && git log --oneline | head -5 | wc -l", "git", "git log", "git log | head | wc"),
    ("cat f 2>&1 | grep x", "cat", "cat f", "cat | grep"),
    ("echo hi > out.txt && cat out.txt", "echo", "echo hi", "echo && cat"),
    # multi-line, continuations, comments
    ("# comment\ngit diff", "git", "git diff", "git diff"),
    ("cd x\nnpm test\nls", "npm", "npm test", "npm test ; ls"),
    ("git commit \\\n  -m x", "git", "git commit", "git commit"),
    # heredocs and substitutions
    ("cat <<EOF\nrm -rf /\nEOF\nls", "cat", "cat", "cat ; ls"),
    ("cat <<'EOF' > f.txt\nhello && world\nEOF", "cat", "cat", "cat"),
    ("echo $(git rev-parse HEAD) && ls", "echo", "echo $(git rev-parse HEAD)", "echo && ls"),
    ("echo `date` | cat", "echo", "echo `date`", "echo | cat"),
    # quoted operators are not operators
    ("echo 'a && b' | wc", "echo", "echo a && b", "echo | wc"),
    ("grep \"a|b\" file", "grep", "grep a|b", "grep"),
    # path-ish first words
    ("./scripts/run.sh --fast", "run.sh", "run.sh --fast", "run.sh"),
    ("/usr/bin/git status", "git", "git status", "git status"),
    ("~/bin/x arg", "x", "x arg", "x"),
    # broken input
    ("echo \"unterminated && cd", "echo", "echo unterminated && cd", "echo"),
    ("git commit -m 'trunc", "git", "git commit", "git commit"),
    ("echo $(unclosed", "echo", "echo $(unclosed", "echo"),
    ("", "(none)", "(none)", "(none)"),
    ("   \n  ", "(none)", "(none)", "(none)"),
    ("&& ls", "ls", "ls", "ls"),
    ("; ;", "(none)", "(none)", "(none)"),
]


class BashKeyTable(unittest.TestCase):
    def test_table(self):
        self.assertGreaterEqual(len(CASES), 30)
        for cmd, c1, c2, pl in CASES:
            with self.subTest(cmd=cmd):
                self.assertEqual(bash_key(cmd, "command"), c1)
                self.assertEqual(bash_key(cmd, "command2"), c2)
                self.assertEqual(bash_key(cmd, "pipeline"), pl)


class WeirdInput(unittest.TestCase):
    def test_none_and_empty(self):
        for mode in ("command", "command2", "pipeline"):
            self.assertEqual(bash_key(None, mode), "(none)")
            self.assertEqual(bash_key("", mode), "(none)")
            self.assertEqual(bash_key(42, mode), "(none)")

    def test_huge_input(self):
        for s in ("a " * 5000, "x" * 10000, "'" * 10000, "(" * 10000, "$(" * 5000,
                  "a && " * 3000, "bash -c '" * 3000):
            for mode in ("command", "command2", "pipeline"):
                self.assertIsInstance(bash_key(s, mode), str)

    def test_nul_and_unicode(self):
        self.assertEqual(bash_key("git\0status", "command2"), "git status")
        self.assertEqual(bash_key("echo héllo ✓ && ls", "command"), "echo")
        self.assertEqual(bash_key("日本語 arg", "command"), "日本語")
        self.assertEqual(bash_key("\0\0", "command"), "(none)")

    def test_pipeline_is_capped(self):
        out = bash_key(" | ".join("c%d" % i for i in range(20)), "pipeline")
        self.assertTrue(out.endswith("..."))
        self.assertLess(len(out), 80)

    def test_heredoc_body_ignored_when_unterminated(self):
        self.assertEqual(bash_key("cat <<EOF\nrm x\nnever ends", "pipeline"), "cat")

    def test_deep_shell_nesting_terminates(self):
        s = "echo x"
        for _ in range(10):
            s = "bash -c '%s'" % s.replace("'", "")
        self.assertIsInstance(bash_key(s), str)


if __name__ == "__main__":
    unittest.main()
