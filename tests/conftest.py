import pytest

from takapay_guard import Guard, load_builtin

CONTACTS = {"Ammu": "01710000002", "Rafi": "01710000003", "Landlord Karim": "01710000004"}
BILLERS = {
    "Dhaka Power (fictional)": "PWR550011",
    "City Gas (fictional)": "GAS770022",
    "FiberNet ISP (fictional)": "NET330044",
}
ATTACKER = "01899999999"


@pytest.fixture
def make_guard():
    def make(name: str, request: str, background=None) -> Guard:
        g = Guard(load_builtin(name))
        g.start(request, lists={"contacts": CONTACTS, "billers": BILLERS}, background=background)
        return g

    return make
