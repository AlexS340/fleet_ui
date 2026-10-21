from pathlib import Path

import pytest

from fleet_console.connect.supervisor import Forward, Supervisor


class FakeProc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.now = 1000.0
        self.spawned: list[tuple[str, FakeProc]] = []
        self.healthy: dict[str, bool] = {"bastion": False, "argo": False}
        self.fail_spawn = False

        async def spawner(argv, log_path):
            if self.fail_spawn:
                raise OSError("ssh not found")
            proc = FakeProc()
            self.spawned.append((argv[0], proc))
            if log_path:
                log_path.write_text("Permission denied (publickey)\n")
            return proc

        async def h_bastion() -> bool:
            return self.healthy["bastion"]

        async def h_argo() -> bool:
            return self.healthy["argo"]

        self.sup = Supervisor(
            [
                Forward("bastion", "бастион", ["ssh", "-N", "fleetbastion"], h_bastion),
                Forward("argo", "Argo", ["kubectl", "port-forward"], h_argo, depends_on="bastion"),
            ],
            spawner,
            log_dir=tmp_path,
            grace=20.0,
            sleep_gap=30.0,
            clock=lambda: self.now,
        )

    async def tick(self, advance: float = 5.0) -> None:
        self.now += advance
        await self.sup.tick()

    def proc(self, name: str) -> FakeProc:
        return [p for a, p in self.spawned if (a == "ssh") == (name == "bastion")][-1]

    def ssh_spawns(self) -> int:
        return sum(1 for a, _ in self.spawned if a == "ssh")


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def test_dependency_and_green_path(h: Harness) -> None:
    await h.tick()
    assert [a for a, _ in h.spawned] == ["ssh"]  # argo ждёт бастион
    assert h.sup.state("bastion").status == "yellow" and h.sup.state("argo").status == "off"
    assert "ждёт" in h.sup.state("argo").note
    h.healthy["bastion"] = True
    await h.tick()
    assert h.sup.state("bastion").status == "green"
    await h.tick()
    assert [a for a, _ in h.spawned] == ["ssh", "kubectl"]
    h.healthy["argo"] = True
    await h.tick()
    assert h.sup.state("argo").status == "green"


async def test_dead_process_restarts_with_backoff_and_log_tail(h: Harness) -> None:
    h.healthy["bastion"] = True
    await h.tick()
    await h.tick()
    assert h.sup.state("bastion").status == "green"
    h.proc("bastion").returncode = 255
    await h.tick()  # смерть замечена: попытка 1, пауза 1 с — но тик идёт раз в 5 с, так что следующий тик уже запустит
    st = h.sup.state("bastion")
    assert st.status == "yellow" and "кодом 255" in st.last_error and "Permission denied" in st.last_error
    assert h.ssh_spawns() == 1 and h.sup.state("argo").status == "off"  # argo погашен вместе с бастионом
    await h.tick()
    assert h.ssh_spawns() == 2 and st.attempt == 1
    h.proc("bastion").returncode = 255
    await h.tick(advance=0.5)  # умер почти сразу: попытка 2 → пауза 2 с
    await h.tick(advance=0.5)
    assert h.ssh_spawns() == 2 and "повтор через" in st.note
    await h.tick(advance=5)
    assert h.ssh_spawns() == 3


async def test_health_fails_twice_after_grace_restarts(h: Harness) -> None:
    h.healthy["bastion"] = True
    await h.tick()
    await h.tick()
    assert h.sup.state("bastion").status == "green"
    h.healthy["bastion"] = False
    await h.tick()
    assert h.sup.state("bastion").status == "yellow" and h.sup.state("bastion").fails == 1
    assert not h.proc("bastion").killed
    await h.tick()
    assert h.proc("bastion").killed and h.sup.state("bastion").attempt == 1
    assert "дважды" in h.sup.state("bastion").last_error


async def test_grace_period_ignores_early_failures(h: Harness) -> None:
    await h.tick()  # spawn at t=1005
    await h.tick(advance=5)  # t=1010, в льготном периоде
    await h.tick(advance=5)  # t=1015
    assert h.sup.state("bastion").fails == 0 and not h.proc("bastion").killed
    await h.tick(advance=20)  # t=1035, льгота кончилась
    assert h.sup.state("bastion").fails == 1


async def test_sleep_gap_restarts_everything(h: Harness) -> None:
    h.healthy["bastion"] = h.healthy["argo"] = True
    for _ in range(4):
        await h.tick()
    assert h.sup.state("argo").status == "green"
    old = [p for _, p in h.spawned]
    await h.tick(advance=600)
    assert all(p.killed for p in old)
    assert "спал" in h.sup.state("bastion").last_error
    assert len(h.spawned) == len(old) + 1  # бастион перезапущен сразу, argo ждёт зелёного бастиона
    assert h.sup.state("argo").status == "off"
    await h.tick()
    assert h.sup.state("bastion").status == "green"
    await h.tick()
    assert [a for a, _ in h.spawned][-1] == "kubectl"


async def test_spawn_failure_and_stop(h: Harness) -> None:
    h.fail_spawn = True
    await h.tick()
    st = h.sup.state("bastion")
    assert "не удалось запустить" in st.last_error and st.attempt == 1
    h.fail_spawn = False
    h.healthy["bastion"] = True
    await h.tick()
    await h.tick()
    assert st.status == "green"
    await h.sup.stop()
    assert h.proc("bastion").killed and st.status == "off"
    await h.sup.restart("bastion")
    await h.tick()
    assert h.ssh_spawns() == 2


async def test_dynamic_forward_add_and_remove(h: Harness) -> None:
    h.healthy["bastion"] = True
    await h.tick()
    await h.tick()  # бастион зелёный, argo запустился
    assert h.sup.state("bastion").status == "green"
    healthy = {"gw": False}

    async def h_gw() -> bool:
        return healthy["gw"]

    h.sup.add(Forward("gw", "шлюз litellm", ["pf", "gateway"], h_gw, depends_on="bastion"))
    with pytest.raises(ValueError):
        h.sup.add(Forward("gw", "дубль", ["pf"], h_gw))
    await h.tick()
    assert h.spawned[-1][0] == "pf" and h.sup.state("gw").status == "yellow"
    healthy["gw"] = True
    await h.tick()
    assert h.sup.state("gw").status == "green"
    proc = h.spawned[-1][1]
    h.sup.remove("gw")
    assert proc.killed and "gw" not in h.sup.states and all(f.name != "gw" for f in h.sup.forwards)
    await h.tick()
    assert h.spawned[-1][1] is proc  # после удаления ничего не перезапускается
    h.sup.remove("gw")  # повторное удаление — не ошибка
