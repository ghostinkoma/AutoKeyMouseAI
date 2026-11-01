from akm.town_move import TOWNS, arrived, find_town, load_towns, move_script


def test_eleven_towns_in_order():
    assert [t.label for t in TOWNS] == ["Lorencia", "Noria", "Devias", "Elbeland", "Lost Tower", "Tarkan", "Aida",
                                        "Kanturu Ruins", "Kanturu Relics", "Raklion", "Crywolf"]


def test_move_script():
    t = find_town(load_towns(), "lorencia")
    assert move_script(t) == "k:enter;w:300;t:/move lorencia;k:enter"
    assert move_script(t, 500, 4000) == "k:enter;w:500;t:/move lorencia;k:enter;w:4000"


def test_config_override_marks_verified():
    towns = load_towns({"town_move": {"commands": {"kanturu_relics": "kanturu3x"}}})
    t = find_town(towns, "kanturu_relics")
    assert t.command == "kanturu3x" and t.verified
    assert find_town(towns, "カントル") is t


def test_arrived():
    towns = load_towns()
    assert arrived(find_town(towns, "losttower"), "Lost Tower 3")
    assert arrived(find_town(towns, "crywolf"), "Crywolf Fortress")
    assert not arrived(find_town(towns, "noria"), "Lorencia")
    assert not arrived(find_town(towns, "noria"), None)
