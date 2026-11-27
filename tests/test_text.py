from takapay_guard.text import extract_amounts, extract_numbers, join_digit_groups, normalise_number, normalise_text


def test_normalise_number_handles_bangla_digits_spaces_and_country_code():
    assert normalise_number("+৮৮ ০১৮৯৯-৯৯৯৯৯৯") == "01899999999"
    assert normalise_number("8801899999999") == "01899999999"
    assert normalise_number(" 01710 000003 ") == "01710000003"
    assert normalise_number("PWR550011") == "PWR550011"


def test_normalise_text_folds_case_digits_and_spaces():
    assert normalise_text("OTP  ৪৮২৯১৩\n") == "otp 482913"


def test_extract_numbers_in_any_script():
    assert extract_numbers("Rafi ke ০১৭১০০০০০০৩ e pathao") == {"01710000003"}
    assert extract_numbers("send to 01899 999999 now") == {"01899999999"}
    assert extract_numbers("no number here, 12345") == set()


def test_extract_amounts_leaves_out_dates():
    assert extract_amounts("বিল ১,৮৫০ টাকা, শেষ তারিখ ১০ অক্টোবর") == {1850.0}
    assert extract_amounts("Pay 450 Tk by 5 tarikh") == {450.0}
    assert extract_amounts("due September 2026, 1200 taka") == {1200.0}


def test_join_digit_groups_finds_spaced_codes():
    assert "482913" in join_digit_groups("code is 482 913")
    assert "482913" in join_digit_groups("কোড ৪৮২-৯১৩")
