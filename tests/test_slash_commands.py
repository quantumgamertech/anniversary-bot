import ast
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
SOURCE = MAIN.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def command_names(prefix):
    names = []
    for node in ast.walk(TREE):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            text = ast.unparse(dec)
            if text.startswith(prefix):
                match = re.search(r'name=["\']([^"\']+)["\']', text)
                if match:
                    names.append(match.group(1))
    return names


def function_source(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"Function not found: {name}")


class SlashCommandMigrationTests(unittest.TestCase):
    def test_expected_slash_commands_registered(self):
        slash = set(command_names("bot.tree.command"))
        expected = {
            "ping", "help", "start", "setup", "about", "invite", "stats", "serverstatus",
            "growthtoday", "analytics", "bestday", "growthleaderboard", "healthscore", "advisor", "growthpredict",
            "setreport", "reportchannel", "setmilestone", "removemilestone", "milestones", "setvoterole",
            "dashboard", "growthweek", "setalertthreshold", "alerts", "premium", "buypremium", "premiumstatus",
            "vote", "votestatus",
        }
        self.assertTrue(expected.issubset(slash))
        self.assertEqual(len(slash), 30)

    def test_no_duplicate_slash_names(self):
        names = command_names("bot.tree.command")
        self.assertEqual(len(names), len(set(names)))

    def test_owner_commands_not_exposed_as_slash(self):
        slash = set(command_names("bot.tree.command"))
        self.assertTrue({"amowner", "setpremium", "removepremium", "testvote", "voteadmin", "servers"}.isdisjoint(slash))

    def test_existing_prefix_commands_remain_registered(self):
        prefix = set(command_names("bot.command"))
        expected = {
            "ping", "help", "setup", "start", "about", "invite", "vote", "votestatus", "stats", "serverstatus",
            "premium", "buypremium", "premiumstatus", "setmilestone", "removemilestone", "milestones", "setreport",
            "setvoterole", "reportchannel", "growthtoday", "analytics", "growthweek", "bestday", "growthleaderboard",
            "dashboard", "setalertthreshold", "alerts", "senddailyreport", "amowner", "setpremium", "removepremium",
            "testvote", "voteadmin", "servers",
        }
        self.assertTrue(expected.issubset(prefix))

    def test_help_contains_new_public_slash_commands(self):
        help_src = function_source("build_help_embed")
        for command in [
            "ping", "help", "start", "setup", "about", "invite", "stats", "serverstatus", "growthtoday",
            "analytics", "bestday", "growthleaderboard", "healthscore", "advisor", "growthpredict", "setreport",
            "reportchannel", "setmilestone", "removemilestone", "milestones", "setvoterole", "dashboard", "growthweek",
            "setalertthreshold", "alerts", "premium", "buypremium", "premiumstatus", "vote", "votestatus",
        ]:
            self.assertIn(f"`/{command}`", help_src)

    def test_public_help_does_not_expose_owner_commands(self):
        help_src = function_source("build_help_embed")
        public_src = help_src.split("if include_owner:", 1)[0]
        for command in ["servers", "setpremium", "removepremium", "voteadmin", "testvote", "amowner"]:
            self.assertNotIn(f"`!{command}", public_src)
            self.assertNotIn(f"`/{command}`", public_src)

    def test_setup_and_config_slash_commands_require_admin(self):
        for function in ["setup_slash", "setreport_slash", "setmilestone_slash", "removemilestone_slash", "setvoterole_slash", "setalertthreshold_slash", "alerts_slash"]:
            self.assertIn("require_slash_admin", function_source(function))

    def test_premium_slash_commands_require_premium(self):
        for function in ["growthweek_slash", "setalertthreshold_slash", "alerts_slash"]:
            self.assertIn("require_slash_premium", function_source(function))

    def test_guild_only_protections_present(self):
        for function in ["serverstatus_slash", "setreport_slash", "reportchannel_slash", "setmilestone_slash", "removemilestone_slash", "milestones_slash", "setvoterole_slash", "bestday_slash", "growthweek_slash", "setalertthreshold_slash", "alerts_slash"]:
            self.assertIn("require_slash_guild", function_source(function))

    def test_channel_and_role_argument_handling(self):
        self.assertIn("channel: discord.TextChannel", function_source("setreport_slash"))
        self.assertIn("permissions_for", function_source("setreport_slash"))
        self.assertIn("role: discord.Role", function_source("setmilestone_slash"))
        self.assertIn("role: Optional[discord.Role] = None", function_source("setvoterole_slash"))

    def test_database_write_helpers_preserve_command_behaviors(self):
        expectations = {
            "build_set_report_embed": "db.set_report_channel",
            "build_set_milestone_embed": "db.set_milestone_role",
            "build_remove_milestone_embed": "db.remove_milestone_role",
            "build_set_vote_role_embed": "db.set_vote_reward_role",
            "build_alert_threshold_updated_embed": "db.set_growth_alert_threshold",
            "build_alerts_updated_embed": "db.set_alerts_enabled",
        }
        for function, call in expectations.items():
            self.assertIn(call, function_source(function))


if __name__ == "__main__":
    unittest.main()
