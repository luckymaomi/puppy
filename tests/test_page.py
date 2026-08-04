from puppy.page import NOTE_PATH_PATTERN, PageGate, classify_page


def test_page_gate_stops_for_security_challenge_before_any_action() -> None:
    assert classify_page("https://www.xiaohongshu.com/explore", "请完成安全验证") == (
        PageGate.HUMAN,
        "安全验证",
    )


def test_page_gate_requires_manual_login() -> None:
    gate, reason = classify_page("https://www.xiaohongshu.com/login", "")
    assert gate == PageGate.LOGIN
    assert reason


def test_login_verification_input_is_not_misclassified_as_security_challenge() -> None:
    gate, _ = classify_page(
        "https://www.xiaohongshu.com/explore",
        "小红书如何扫码 输入手机号 输入验证码 获取验证码 登录",
    )
    assert gate == PageGate.LOGIN


def test_note_id_is_accepted_only_from_known_visible_page_paths() -> None:
    valid = NOTE_PATH_PATTERN.search("/search_result/64d73b70c2133c0001abcd12")
    invalid = NOTE_PATH_PATTERN.search("/user/profile/64d73b70c2133c0001abcd12")
    assert valid and valid.group(1) == "64d73b70c2133c0001abcd12"
    assert invalid is None
