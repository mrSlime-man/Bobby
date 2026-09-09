import yaml

from linkedin_messages import load_messages_config


def test_load_messages_config_returns_defaults_when_file_missing(tmp_path):
    config = load_messages_config(tmp_path / "missing.yaml")

    assert config["dry_run"] is True
    assert config["auto_star_job_offers"] is True
    assert config["reply_tone"] == "a thoughtful senior engineering leader"
    assert config["old_message_threshold_days"] == 60


def test_load_messages_config_reads_customized_preferences(tmp_path):
    config_file = tmp_path / "linkedin_messages.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "dry_run": False,
                "execute_archives": True,
                "execute_replies": True,
                "max_conversations_to_scan": 10,
                "unread_only": True,
                "auto_star_job_offers": False,
                "auto_label_job_offers": False,
                "skip_drafting_for_personal_messages": False,
                "reply_tone": "a concise founder",
                "reply_max_characters": 300,
                "reply_short_paragraphs": False,
                "reply_avoid_em_dash": False,
                "old_message_threshold_days": 45,
                "old_message_apology_enabled": True,
                "old_message_apology_reason": "you were heads down on client work",
                "old_job_message_follow_up_enabled": False,
                "old_job_message_follow_up_text": "ask whether the role is still open",
            }
        ),
        encoding="utf-8",
    )

    config = load_messages_config(config_file)

    assert config["dry_run"] is False
    assert config["execute_archives"] is True
    assert config["execute_replies"] is True
    assert config["max_conversations_to_scan"] == 10
    assert config["unread_only"] is True
    assert config["auto_star_job_offers"] is False
    assert config["auto_label_job_offers"] is False
    assert config["skip_drafting_for_personal_messages"] is False
    assert config["reply_tone"] == "a concise founder"
    assert config["reply_max_characters"] == 300
    assert config["reply_short_paragraphs"] is False
    assert config["reply_avoid_em_dash"] is False
    assert config["old_message_threshold_days"] == 45
    assert config["old_message_apology_reason"] == "you were heads down on client work"
    assert config["old_job_message_follow_up_enabled"] is False
    assert config["old_job_message_follow_up_text"] == "ask whether the role is still open"
