# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2019-2020 EdNoepel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import pytest
import threading
import time

from auction_keeper.logic import Reservoir
from auction_keeper.main import AuctionKeeper
from pymaker.numeric import Wad, Ray, Rad
from tests.conftest import keeper_address, mcd, our_address, web3, wrap_eth, purchase_usdv
from tests.helper import args, kill_other_threads


class TestVatUsdv:
    def setup_method(self):
        self.web3 = web3()
        self.mcd = mcd(web3())
        self.keeper_address = keeper_address(web3())
        self.mcd.approve_usdv(self.keeper_address)
        self.our_address = our_address(web3())
        self.mcd.approve_usdv(self.our_address)
        self.collateral = self.mcd.collaterals['VLX-A']

    def get_usdv_token_balance(self) -> Wad:
        return self.mcd.usdv.balance_of(self.keeper_address)

    def get_usdv_vat_balance(self) -> Wad:
        return Wad(self.mcd.vat.usdv(self.keeper_address))

    def get_gem_token_balance(self) -> Wad:
        return self.collateral.gem.balance_of(self.keeper_address)

    def get_gem_vat_balance(self) -> Wad:
        return self.mcd.vat.gem(self.collateral.ilk, self.keeper_address)

    def give_away_usdv(self):
        assert self.mcd.web3.eth.defaultAccount == self.keeper_address.address
        assert self.mcd.usdv_adapter.exit(self.keeper_address, self.get_usdv_vat_balance())
        assert self.mcd.usdv.transfer(self.our_address, self.get_usdv_token_balance()).transact()


class TestVatUsdvTarget(TestVatUsdv):
    def create_keeper(self, usdv: float):
        assert isinstance(usdv, float)
        keeper = AuctionKeeper(args=args(f"--eth-from {self.keeper_address} "
                                         f"--type flop "
                                         f"--from-block 1 "
                                         f"--vat-dai-target {usdv} "
                                         f"--model ./bogus-model.sh"), web3=self.web3)
        assert self.web3.eth.defaultAccount == self.keeper_address.address
        keeper.startup()
        return keeper

    def create_keeper_join_all(self):
        keeper = AuctionKeeper(args=args(f"--eth-from {self.keeper_address} "
                                         f"--type flop "
                                         f"--from-block 1 "
                                         f"--vat-dai-target all "
                                         f"--model ./bogus-model.sh"), web3=self.web3)
        assert self.web3.eth.defaultAccount == self.keeper_address.address
        keeper.startup()
        return keeper

    def test_no_change(self):
        # given balances before
        token_balance_before = self.get_usdv_token_balance()
        vat_balance_before = self.get_usdv_vat_balance()

        # when rebalancing with the current vat amount
        self.create_keeper(float(vat_balance_before))

        # then ensure no balances changed
        assert token_balance_before == self.get_usdv_token_balance()
        assert vat_balance_before == self.get_usdv_vat_balance()

    def test_join_enough(self, keeper_address):
        # given purchasing some usdv
        purchase_usdv(Wad.from_number(237), keeper_address)
        token_balance_before = self.get_usdv_token_balance()
        assert token_balance_before == Wad.from_number(237)

        # when rebalancing with a smaller amount than we have
        self.create_keeper(153.0)

        # then ensure usdv was joined to the vat
        assert token_balance_before > self.get_usdv_token_balance()
        assert self.get_usdv_vat_balance() == Wad.from_number(153)

    def test_join_not_enough(self):
        # given balances before
        assert self.get_usdv_token_balance() == Wad.from_number(84)
        assert self.get_usdv_vat_balance() == Wad.from_number(153)

        # when rebalancing without enough tokens to cover the difference
        self.create_keeper(500.0)

        # then ensure all available tokens were joined
        assert self.get_usdv_token_balance() == Wad(0)
        assert self.get_usdv_vat_balance() == Wad.from_number(237)

    def test_exit_some(self):
        # given balances before
        assert self.get_usdv_token_balance() == Wad(0)
        assert self.get_usdv_vat_balance() == Wad.from_number(237)

        # when rebalancing to a smaller amount than currently in the vat
        self.create_keeper(200.0)

        # then ensure balances are as expected
        assert self.get_usdv_token_balance() == Wad.from_number(37)
        assert self.get_usdv_vat_balance() == Wad.from_number(200)

    def test_exit_all(self):
        # given balances before
        assert self.get_usdv_token_balance() == Wad.from_number(37)
        assert self.get_usdv_vat_balance() == Wad.from_number(200)

        # when rebalancing to 0
        self.create_keeper(0.0)

        # then ensure all usdv has been exited
        assert self.get_usdv_token_balance() == Wad.from_number(237)
        assert self.get_usdv_vat_balance() == Wad(0)

    def test_join_all(self):
        # given usdv we just exited
        token_balance_before = self.get_usdv_token_balance()
        assert token_balance_before == Wad.from_number(237)

        # when keeper is started with a token balance
        self.create_keeper_join_all()

        # then ensure all available tokens were joined
        assert self.get_usdv_token_balance() == Wad(0)
        assert self.get_usdv_vat_balance() == Wad.from_number(237)


class TestEmptyVatOnExit(TestVatUsdv):
    def create_keeper(self, exit_usdv_on_shutdown: bool, exit_gem_on_shutdown: bool):
        assert isinstance(exit_usdv_on_shutdown, bool)
        assert isinstance(exit_gem_on_shutdown, bool)

        vat_usdv_behavior = "" if exit_usdv_on_shutdown else "--keep-dai-in-vat-on-exit"
        vat_gem_behavior = "" if exit_gem_on_shutdown else "--keep-gem-in-vat-on-exit"

        keeper = AuctionKeeper(args=args(f"--eth-from {self.keeper_address} "
                                         f"--type flip --ilk {self.collateral.ilk.name} "
                                         f"--from-block 1 "
                                         f"{vat_usdv_behavior} "
                                         f"{vat_gem_behavior} "
                                         f"--model ./bogus-model.sh"), web3=self.web3)
        self.web3 = keeper.web3
        self.mcd = keeper.mcd
        assert self.web3.eth.defaultAccount == self.keeper_address.address
        assert keeper.arguments.exit_usdv_on_shutdown == exit_usdv_on_shutdown
        assert keeper.arguments.exit_gem_on_shutdown == exit_gem_on_shutdown
        keeper.startup()
        return keeper

    def test_do_not_empty(self):
        # given usdv and gem in the vat
        keeper = self.create_keeper(False, False)
        purchase_usdv(Wad.from_number(153), self.keeper_address)
        assert self.get_usdv_token_balance() >= Wad.from_number(153)
        assert self.mcd.usdv_adapter.join(self.keeper_address, Wad.from_number(153)).transact(
            from_address=self.keeper_address)
        wrap_eth(self.mcd, self.keeper_address, Wad.from_number(6))
        # and balances before
        usdv_token_balance_before = self.get_usdv_token_balance()
        usdv_vat_balance_before = self.get_usdv_vat_balance()
        gem_token_balance_before = self.get_gem_token_balance()
        gem_vat_balance_before = self.get_gem_vat_balance()

        # when creating and shutting down the keeper
        keeper.shutdown()

        # then ensure no balances changed
        assert usdv_token_balance_before == self.get_usdv_token_balance()
        assert usdv_vat_balance_before == self.get_usdv_vat_balance()
        assert gem_token_balance_before == self.get_gem_token_balance()
        assert gem_vat_balance_before == self.get_gem_vat_balance()

    def test_empty_usdv_only(self):
        # given balances before
        usdv_token_balance_before = self.get_usdv_token_balance()
        usdv_vat_balance_before = self.get_usdv_vat_balance()
        gem_token_balance_before = self.get_gem_token_balance()
        gem_vat_balance_before = self.get_gem_vat_balance()

        # when creating and shutting down the keeper
        keeper = self.create_keeper(True, False)
        keeper.shutdown()

        # then ensure the usdv was emptied
        assert self.get_usdv_token_balance() == usdv_token_balance_before + usdv_vat_balance_before
        assert self.get_usdv_vat_balance() == Wad(0)
        # and gem was not emptied
        assert gem_token_balance_before == self.get_gem_token_balance()
        assert gem_vat_balance_before == self.get_gem_vat_balance()

    def test_empty_gem_only(self):
        # given gem balances before
        gem_token_balance_before = self.get_gem_token_balance()
        gem_vat_balance_before = self.get_gem_vat_balance()

        # when adding usdv
        purchase_usdv(Wad.from_number(79), self.keeper_address)
        assert self.mcd.usdv_adapter.join(self.keeper_address, Wad.from_number(79)).transact(
            from_address=self.keeper_address)
        usdv_token_balance_before = self.get_usdv_token_balance()
        usdv_vat_balance_before = self.get_usdv_vat_balance()
        # and creating and shutting down the keeper
        keeper = self.create_keeper(False, True)
        keeper.shutdown()

        # then ensure usdv was not emptied
        assert usdv_token_balance_before == self.get_usdv_token_balance()
        assert usdv_vat_balance_before == self.get_usdv_vat_balance()
        # and gem was emptied
        assert gem_token_balance_before == gem_token_balance_before + gem_vat_balance_before
        assert self.get_gem_vat_balance() == Wad(0)

    def test_empty_both(self):
        # when creating and shutting down the keeper
        keeper = self.create_keeper(True, True)
        keeper.shutdown()

        # then ensure the vat is empty
        assert self.get_usdv_vat_balance() == Wad(0)
        assert self.get_gem_vat_balance() == Wad(0)

        # clean up
        self.give_away_usdv()


class TestRebalance(TestVatUsdv):
    def create_keeper(self, mocker, usdv_target="all"):
        # Create a keeper
        mocker.patch("web3.net.Net.peer_count", return_value=1)
        self.keeper = AuctionKeeper(args=args(f"--eth-from {self.keeper_address} "
                                         f"--type flip --ilk VLX-A --bid-only "
                                         f"--vat-dai-target {usdv_target} "
                                         f"--return-gem-interval 3 "
                                         f"--model ./bogus-model.sh"), web3=self.web3)
        assert self.web3.eth.defaultAccount == self.keeper_address.address
        self.web3 = self.keeper.web3
        self.mcd = self.keeper.mcd
        assert self.keeper.auctions
        self.collateral = self.keeper.collateral
        self.collateral.approve(self.keeper_address)

        self.thread = threading.Thread(target=self.keeper.main, daemon=True)
        self.thread.start()
        return self.keeper

    def shutdown_keeper(self):
        self.keeper.shutdown()  # HACK: Lifecycle doesn't invoke this as expected
        self.keeper.lifecycle.terminate("unit test completed")
        self.thread.join()

        # HACK: Lifecycle leaks threads; this needs to be fixed in pymaker
        kill_other_threads()

        assert self.get_usdv_vat_balance() == Wad(0)

    @pytest.mark.timeout(60)
    def test_balance_added_after_startup(self, mocker):
        try:
            # given gem balances after starting keeper
            token_balance_before = self.get_usdv_token_balance()
            self.create_keeper(mocker)
            time.sleep(6)  # wait for keeper to join everything on startup
            vat_balance_before = self.get_usdv_vat_balance()
            assert self.get_usdv_token_balance() == Wad(0)
            assert vat_balance_before == Wad(0)

            # when adding Usdv
            purchase_usdv(Wad.from_number(77), self.keeper_address)
            assert self.get_usdv_token_balance() == Wad.from_number(77)
            # and pretending there's a bid which requires Usdv
            reservoir = Reservoir(self.keeper.vat.usdv(self.keeper_address))
            assert self.keeper.check_bid_cost(id=1, cost=Rad.from_number(20), reservoir=reservoir)

            # then ensure all Usdv is joined
            assert self.get_usdv_token_balance() == Wad(0)
            assert self.get_usdv_vat_balance() == Wad.from_number(77)

            # when adding more Usdv and pretending there's a bid we cannot cover
            purchase_usdv(Wad.from_number(23), self.keeper_address)
            assert self.get_usdv_token_balance() == Wad.from_number(23)
            reservoir = Reservoir(self.keeper.vat.usdv(self.keeper_address))
            assert not self.keeper.check_bid_cost(id=2, cost=Rad(Wad.from_number(120)), reservoir=reservoir)

            # then ensure the added Usdv was joined anyway
            assert self.get_usdv_token_balance() == Wad(0)
            assert self.get_usdv_vat_balance() == Wad.from_number(100)

        finally:
            self.shutdown_keeper()
            self.give_away_usdv()

    @pytest.mark.timeout(600)
    def test_fixed_usdv_target(self, mocker):
        try:
            # given a keeper configured to maintained a fixed amount of Usdv
            target = Wad.from_number(100)
            purchase_usdv(target * 2, self.keeper_address)
            assert self.get_usdv_token_balance() == Wad.from_number(200)

            self.create_keeper(mocker, target)
            time.sleep(6)  # wait for keeper to join 100 on startup
            vat_balance_before = self.get_usdv_vat_balance()
            assert vat_balance_before == target

            # when spending Usdv
            assert self.keeper.usdv_join.exit(self.keeper_address, Wad.from_number(22)).transact()
            assert self.get_usdv_vat_balance() == Wad.from_number(78)
            # and pretending there's a bid which requires more Usdv
            reservoir = Reservoir(self.keeper.vat.usdv(self.keeper_address))
            assert self.keeper.check_bid_cost(id=3, cost=Rad.from_number(79), reservoir=reservoir)

            # then ensure Usdv was joined up to the target
            assert self.get_usdv_vat_balance() == target

            # when pretending there's a bid which we have plenty of Usdv to cover
            reservoir = Reservoir(self.keeper.vat.usdv(self.keeper_address))
            assert self.keeper.check_bid_cost(id=4, cost=Rad(Wad.from_number(1)), reservoir=reservoir)

            # then ensure Usdv levels haven't changed
            assert self.get_usdv_vat_balance() == target

        finally:
            self.shutdown_keeper()

    @pytest.mark.timeout(30)
    def test_collateral_removal(self, mocker):
        try:
            # given a keeper configured to return all collateral upon rebalance
            token_balance_before = self.get_gem_token_balance()
            vat_balance_before = self.get_gem_vat_balance()
            self.create_keeper(mocker)
            time.sleep(6)  # wait for keeper to startup
            assert self.get_gem_token_balance() == token_balance_before
            assert self.get_gem_vat_balance() == vat_balance_before

            # when some VLX was wrapped and joined
            wrap_eth(self.mcd, self.keeper_address, Wad.from_number(1.53))
            token_balance = self.get_gem_token_balance()
            assert token_balance > Wad(0)
            self.collateral.adapter.join(self.keeper_address, token_balance).transact()
            assert self.get_gem_vat_balance() == vat_balance_before + token_balance

            # then wait to ensure collateral was exited automatically
            time.sleep(4)
            assert self.get_gem_vat_balance() == Wad(0)
            assert self.get_gem_token_balance() == token_balance_before + Wad.from_number(1.53)

        finally:
            self.shutdown_keeper()
