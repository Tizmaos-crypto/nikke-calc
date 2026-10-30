"""Temporary Queen support: behavior checks, without changing existing goldens."""
from __future__ import annotations

from collections import Counter
import unittest
from unittest.mock import patch

from calculator.buff_manager import BuffManager
from calculator.timeline import simulate
from runner import spec

QUEEN = "마리안 : 퀸"
SQUAD = ["리틀 머메이드", "크라운", QUEEN, "test_B3"]
SPEAR = "다크 매터 창"
WOUND = "약점 개방"
MODE = "임전 : 파고드는 소망"


def run(*, duration=30, no_burst=False, enemy=None, reverse=False):
    names = SQUAD[:2] + (["test_B3", QUEEN] if reverse else [QUEEN, "test_B3"])
    config = {"duration": duration, "rng_mode": "expected"}
    if no_burst:
        config["no_burst_char"] = QUEEN
    return simulate(spec.build_squad(names), config=config, enemy=enemy, verbose=True, seed=1)


class QueenMechanics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.standard = run()
        cls.disabled = run(no_burst=True)

    def test_spear_packets_are_seven_and_only_later_hits_receive_first_wound(self):
        hits = [h for h in self.standard.hits if h.caster == QUEEN and h.skill_name == SPEAR]
        self.assertTrue(hits)
        self.assertEqual(set(Counter(h.t for h in hits).values()), {7})
        packet = [h for h in hits if h.t == 0]
        self.assertEqual(len(packet), 7)
        self.assertGreater(packet[1].damage, packet[0].damage)
        self.assertAlmostEqual(packet[1].damage / packet[0].damage, 1.0505, places=4)
        self.assertEqual(len({h.damage for h in packet[1:]}), 1)

    def test_without_own_burst_four_second_spear_and_no_mode_or_core_skill(self):
        hits = [h for h in self.disabled.hits if h.caster == QUEEN]
        times = sorted({h.t for h in hits if h.skill_name == SPEAR})
        self.assertTrue(all(abs((b-a)-4) < .02 for a, b in zip(times, times[1:])))
        self.assertFalse(any(h.skill_name == "유사 세계" for h in hits))
        own = [e for e in self.disabled.log.buff_events if e.caster == QUEEN]
        self.assertFalse(any(e.name in (MODE, "유사 세계 2", "머나먼 신세계 2", "머나먼 신세계 3", "머나먼 신세계 4") for e in own))

    def test_full_burst_branches_are_exclusive_and_latched_for_ten_seconds(self):
        events = [e for e in self.standard.log.buff_events if e.caster == QUEEN and e.kind == "activate"]
        core = [e for e in events if e.name == "유사 세계 4"]
        seq = [e for e in events if e.name == "유사 세계 3"]
        self.assertTrue(core and seq)
        self.assertTrue({e.t for e in core}.isdisjoint(e.t for e in seq))
        for e in core + seq:
            self.assertAlmostEqual(e.expires_at-e.t, 10)
            self.assertAlmostEqual(e.value, 24.64 if e.name.endswith("4") else 85.17)
        reverse = run(reverse=True, duration=20)
        first = next(e for e in reverse.log.buff_events if e.caster == QUEEN and e.name in ("유사 세계 3", "유사 세계 4") and e.kind == "activate")
        self.assertEqual(first.name, "유사 세계 3")

    def test_burst_buffs_and_refill_are_applied(self):
        expected = {"유사 세계 2": -2, "머나먼 신세계 2": 100, "머나먼 신세계 3": 78.4, "머나먼 신세계 4": 78.14}
        for name, value in expected.items():
            e = next(e for e in self.standard.log.buff_events if e.caster == QUEEN and e.name == name and e.kind == "activate")
            self.assertAlmostEqual(e.value, value)
            self.assertAlmostEqual(e.expires_at-e.t, 10)
        self.assertTrue(any(e.caster == QUEEN and e.stat == "ammo_charge_pct" and e.value == 100 for e in self.standard.log.instant_events))
        self.assertTrue(any(h.caster == QUEEN and h.hit_tag == "burst_damage" for h in self.standard.hits))

    def test_core_skill_survives_absent_geometric_core(self):
        r = run(duration=15, enemy={"core_px": 0})
        own = [h for h in r.hits if h.caster == QUEEN]
        self.assertTrue(any(h.skill_name == "유사 세계" and h.damage > 0 for h in own))
        self.assertFalse(any(h.hit_tag == "core" for h in own))

    def test_named_hits_only_wound_the_landed_target(self):
        # Spawn after the first packet. No burst: new adds are not automatically wounded.
        enemy = {"patterns": [{"id": "adds", "kind": "summon", "after": ["start"], "delay": 1,
            "until": {"time": 8}, "spec": {"count": 3, "hit_hp": 1000000}}]}
        received = []
        original = BuffManager.notify
        def observe(bm, event, t, caster, **ctx):
            if caster == QUEEN and event == f"hit_count:{SPEAR}":
                received.append((t, ctx.get("hit_target")))
            return original(bm, event, t, caster, **ctx)
        with patch.object(BuffManager, "notify", observe):
            r = run(duration=8, no_burst=True, enemy=enemy)
        self.assertTrue(received)
        self.assertTrue(all(target and len(target) == 1 for _, target in received))
        # All seven boss hits of the first packet use precisely the boss context.
        self.assertEqual([target for t, target in received if t == 0], [("__enemy__",)] * 7)
        self.assertTrue(any(target[0].startswith("__enemy__:adds#") for _, target in received))
        self.assertTrue(any(e.caster == QUEEN and e.name == WOUND for e in r.log.buff_events))


    def test_strict_filter_does_not_replace_an_empty_wound_set_with_the_boss(self):
        with patch.object(BuffManager, "enemy_has_state", return_value=False):
            r = run(duration=15)
        self.assertFalse(any(h.caster == QUEEN and h.skill_name == "유사 세계" for h in r.hits))

    def test_shield_blocked_packets_do_not_emit_wound_hits(self):
        enemy = {"patterns": [{"id": "shield", "kind": "shield", "after": ["start"],
                               "delay": 1, "until": {"time": 9}, "code": "수냉"}]}
        received = []
        original = BuffManager.notify
        def observe(bm, event, t, caster, **ctx):
            if caster == QUEEN and event == f"hit_count:{SPEAR}":
                received.append(t)
            return original(bm, event, t, caster, **ctx)
        with patch.object(BuffManager, "notify", observe):
            run(duration=9, no_burst=True, enemy=enemy)
        self.assertEqual(received, [0] * 7)

    def test_sequential_selection_does_not_hit_an_add_already_killed_in_this_packet(self):
        enemy = {"patterns": [{"id": "adds", "kind": "summon", "after": ["start"],
            "delay": 4, "until": {"time": 2}, "spec": {"count": 3, "hit_hp": 1}}]}
        received = []
        original = BuffManager.notify
        def observe(bm, event, t, caster, **ctx):
            if caster == QUEEN and event == f"hit_count:{SPEAR}" and 4 <= t < 4.1:
                received.extend(ctx.get("hit_target", ()))
            return original(bm, event, t, caster, **ctx)
        with patch.object(BuffManager, "notify", observe):
            run(duration=5, no_burst=True, enemy=enemy)
        adds = [x for x in received if x != "__enemy__"]
        self.assertTrue(adds)
        self.assertEqual(len(adds), len(set(adds)))
        self.assertEqual(len(received), 7)


class QueenBuffManager(unittest.TestCase):
    def setUp(self):
        self.bm = BuffManager(spec.build_squad([QUEEN]))
        self.damage = []
        self.bm.register_damage_handler(lambda eff, caster, t: self.damage.append((eff["name"], t)))
        self.bm.notify("battle_start", 0, QUEEN)

    def test_missing_hit_context_never_wounds_all_enemies(self):
        self.bm.notify(f"hit_count:{SPEAR}", .1, QUEEN)
        self.assertFalse(self.bm.enemy_has_state("__enemy__", WOUND))
        self.assertEqual(self.bm._resolve_target(f"enemies_with_buff_strict:{WOUND}", QUEEN), [])

    def test_permanent_wound_unions_targets_without_stacking_in_either_order(self):
        for burst_first in (False, True):
            with self.subTest(burst_first=burst_first):
                bm = BuffManager(spec.build_squad([QUEEN]))
                bm.enemy_resolver = lambda target, caster: ["__enemy__", "__enemy__:add#1"] if target == "all_enemies" else None
                calls = [("burst_cast", {}), (f"hit_count:{SPEAR}", {"hit_target": ("__enemy__",)})]
                if not burst_first:
                    calls.reverse()
                for event, ctx in calls * 3:
                    bm.notify(event, .1, QUEEN, **ctx)
                for enemy in ("__enemy__", "__enemy__:add#1"):
                    self.assertAlmostEqual(bm.get_buffs(QUEEN, enemy, .1)["received_dmg"], 5.05)
                states = [a for a in bm._active if a.effect.get("name") == WOUND]
                self.assertEqual(len(states), 1)
                self.assertEqual(set(states[0].target_chars), {"__enemy__", "__enemy__:add#1"})

    def test_normal_attack_counter_gates_core_skill_without_recursive_skill_counts(self):
        self.bm.notify("on_attack", .1, QUEEN)
        self.bm.notify("burst_cast", 1, QUEEN)
        self.damage.clear()
        self.bm.notify("on_attack", 1.1, QUEEN)
        self.assertEqual(self.damage, [("유사 세계", 1.1)])
        self.bm.notify(f"hit_count:{SPEAR}", 1.2, QUEEN, hit_target=("__enemy__",))
        self.bm.notify("on_attack", 1.3, QUEEN)
        self.assertEqual(len(self.damage), 1)
        self.bm.tick(11)
        self.bm.notify("on_attack", 11.1, QUEEN)
        self.assertEqual(sum(name == "유사 세계" for name, _ in self.damage), 1)

    def test_repeat_timer_shortens_and_restores_remaining_fraction(self):
        self.bm.tick(0)
        spear = next(e for e in self.bm.char_effects(QUEEN) if e["name"] == SPEAR)
        self.assertEqual(self.bm._next_fire[id(spear)], (4, 4))
        self.bm.notify("burst_cast", 1, QUEEN)
        self.bm.tick(1)
        self.assertEqual(self.bm._next_fire[id(spear)], (2.5, 2))
        self.bm.tick(2.5)
        self.assertEqual(self.bm._next_fire[id(spear)], (4.5, 2))
        # A continuous frame loop is important: no skipped timer ticks.
        for frame in range(151, 661):
            self.bm.tick(frame / 60)
        self.assertEqual(self.bm._next_fire[id(spear)][1], 4)


if __name__ == "__main__":
    unittest.main()
