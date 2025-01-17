# This file is part of Maker Keeper Framework.
#
# Copyright (C) 2018-2019 reverendus, bargst, EdNoepel
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

from auction_keeper.gas import DynamicGasPrice
from auction_keeper.main import AuctionKeeper
from auction_keeper.model import Parameters
from auction_keeper.strategy import FlopperStrategy
from datetime import datetime, timezone
from pymaker import Address
from pymaker.approval import hope_directly
from pymaker.auctions import Flopper
from pymaker.deployment import DssDeployment
from pymaker.numeric import Wad, Ray, Rad
from tests.conftest import bite, create_unsafe_cdp, flog_and_heal, gal_address, get_collateral_price, keeper_address, \
    liquidate_urn, mcd, models, our_address, other_address, repay_urn, reserve_usdv, set_collateral_price, \
    simulate_model_output, web3

from tests.helper import args, time_travel_by, wait_for_other_threads, TransactionIgnoringTest
from web3 import Web3


@pytest.fixture(scope="session")
def c(mcd):
    return mcd.collaterals['VLX-A']


@pytest.fixture()
def kick(web3: Web3, mcd: DssDeployment, gal_address, other_address) -> int:
    joy = mcd.vat.usdv(mcd.vow.address)
    woe = (mcd.vat.sin(mcd.vow.address) - mcd.vow.sin()) - mcd.vow.ash()
    print(f'joy={str(joy)[:6]}, woe={str(woe)[:6]}')

    if woe < joy:
        # Bite gal CDP
        c = mcd.collaterals['VLX-A']
        unsafe_cdp = create_unsafe_cdp(mcd, c, Wad.from_number(2), other_address, draw_usdv=False)
        flip_kick = bite(mcd, c, unsafe_cdp)

        # Generate some Usdv, bid on and win the flip auction without covering all the debt
        reserve_usdv(mcd, c, gal_address, Wad.from_number(100))
        c.flipper.approve(mcd.vat.address, approval_function=hope_directly(from_address=gal_address))
        current_bid = c.flipper.bids(flip_kick)
        bid = Rad.from_number(1.9)
        assert mcd.vat.usdv(gal_address) > bid
        assert c.flipper.tend(flip_kick, current_bid.lot, bid).transact(from_address=gal_address)
        time_travel_by(web3, c.flipper.ttl()+1)
        assert c.flipper.deal(flip_kick).transact()

    flog_and_heal(web3, mcd, past_blocks=1200, kiss=False)

    # Kick off the flop auction
    woe = (mcd.vat.sin(mcd.vow.address) - mcd.vow.sin()) - mcd.vow.ash()
    assert mcd.vow.sump() <= woe
    assert mcd.vat.usdv(mcd.vow.address) == Rad(0)
    assert mcd.vow.flop().transact(from_address=gal_address)
    return mcd.flopper.kicks()


@pytest.mark.timeout(600)
class TestAuctionKeeperFlopper(TransactionIgnoringTest):
    @classmethod
    def setup_class(cls):
        cls.web3 = web3()
        cls.our_address = our_address(cls.web3)
        cls.gal_address = gal_address(cls.web3)
        cls.keeper_address = keeper_address(cls.web3)
        cls.other_address = other_address(cls.web3)
        cls.mcd = mcd(cls.web3)
        cls.flopper = cls.mcd.flopper
        cls.flopper.approve(cls.mcd.vat.address, approval_function=hope_directly(from_address=cls.keeper_address))
        cls.flopper.approve(cls.mcd.vat.address, approval_function=hope_directly(from_address=cls.other_address))
    
    def setup_method(self):
        self.keeper = AuctionKeeper(args=args(f"--eth-from {self.keeper_address} "
                                              f"--type flop "
                                              f"--from-block 1 "
                                              f"--model ./bogus-model.sh"), web3=self.web3)
        self.keeper.approve()

        assert isinstance(self.keeper.gas_price, DynamicGasPrice)
        self.default_gas_price = self.keeper.gas_price.get_gas_price(0)

        reserve_usdv(self.mcd, self.mcd.collaterals['VLX-C'], self.keeper_address, Wad.from_number(200.00000))
        reserve_usdv(self.mcd, self.mcd.collaterals['VLX-C'], self.other_address, Wad.from_number(200.00000))

        self.sump = self.mcd.vow.sump()  # Rad

    def teardown_method(self):
        c = self.mcd.collaterals['VLX-A']
        set_collateral_price(self.mcd, c, Wad.from_number(200.00))

    def dent(self, id: int, address: Address, lot: Wad, bid: Rad):
        assert (isinstance(id, int))
        assert (isinstance(lot, Wad))
        assert (isinstance(bid, Rad))

        assert self.flopper.live() == 1

        current_bid = self.flopper.bids(id)
        assert current_bid.guy != Address("0x0000000000000000000000000000000000000000")
        assert current_bid.tic > datetime.now().timestamp() or current_bid.tic == 0
        assert current_bid.end > datetime.now().timestamp()

        assert bid == current_bid.bid
        assert Wad(0) < lot < current_bid.lot
        assert self.flopper.beg() * lot <= current_bid.lot

        assert self.flopper.dent(id, lot, bid).transact(from_address=address)

    def lot_implies_price(self, kick: int, price: Wad) -> bool:
        return round(Rad(self.flopper.bids(kick).lot), 2) == round(self.sump / Rad(price), 2)

    def test_should_detect_flop(self, web3, c, mcd, other_address, keeper_address):
        # given a count of flop auctions
        reserve_usdv(mcd, c, keeper_address, Wad.from_number(230))
        kicks = mcd.flopper.kicks()

        # and an undercollateralized CDP is bitten
        unsafe_cdp = create_unsafe_cdp(mcd, c, Wad.from_number(5), other_address, draw_usdv=False)
        assert mcd.cat.bite(unsafe_cdp.ilk, unsafe_cdp).transact()

        # when the auction ends without debt being covered
        time_travel_by(web3, c.flipper.tau() + 1)

        # then ensure testchain is in the appropriate state
        joy = mcd.vat.usdv(mcd.vow.address)
        awe = mcd.vat.sin(mcd.vow.address)
        woe = (mcd.vat.sin(mcd.vow.address) - mcd.vow.sin()) - mcd.vow.ash()
        sin = mcd.vow.sin()
        sump = mcd.vow.sump()
        wait = mcd.vow.wait()
        assert joy < awe
        assert woe + sin >= sump
        assert wait == 0

        # when
        self.keeper.check_flop()
        wait_for_other_threads()

        # then ensure another flop auction was kicked off
        kick = mcd.flopper.kicks()
        assert kick == kicks + 1

        # clean up by letting someone else bid and waiting until the auction ends
        self.dent(kick, self.other_address, Wad.from_number(0.000012), self.sump)
        time_travel_by(web3, mcd.flopper.ttl() + 1)

    def test_should_start_a_new_model_and_provide_it_with_info_on_auction_kick(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once_with(Parameters(auction_contract=self.keeper.mcd.flopper, id=kick))
        # and
        status = model.send_status.call_args[0][0]
        assert status.id == kick
        assert status.flipper is None
        assert status.flapper is None
        assert status.flopper == self.flopper.address
        assert status.bid > Rad.from_number(0)
        assert status.lot == self.mcd.vow.dump()
        assert status.tab is None
        assert status.beg > Wad.from_number(1)
        assert status.guy == self.mcd.vow.address
        assert status.era > 0
        assert status.end < status.era + self.flopper.tau() + 1
        assert status.tic == 0
        assert status.price == Wad(status.bid / Rad(status.lot))

    def test_should_provide_model_with_updated_info_after_our_own_bid(self):
        # given
        kick = self.flopper.kicks()
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert model.send_status.call_count == 1

        # when
        price = Wad.from_number(50.0)
        simulate_model_output(model=model, price=price)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert model.send_status.call_count > 1
        last_bid = self.flopper.bids(kick)
        # and
        status = model.send_status.call_args[0][0]
        assert status.id == kick
        assert status.flipper is None
        assert status.flapper is None
        assert status.flopper == self.flopper.address
        assert status.bid == last_bid.bid
        assert status.lot == Wad(last_bid.bid / Rad(price))
        assert status.tab is None
        assert status.beg > Wad.from_number(1)
        assert status.guy == self.keeper_address
        assert status.era > 0
        assert status.end > status.era
        assert status.tic > status.era
        assert status.price == price

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_provide_model_with_updated_info_after_somebody_else_bids(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert model.send_status.call_count == 1

        # when
        lot = Wad.from_number(0.0000001)
        assert self.flopper.dent(kick, lot, self.sump).transact(from_address=self.other_address)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert model.send_status.call_count > 1
        # and
        status = model.send_status.call_args[0][0]
        assert status.id == kick
        assert status.flipper is None
        assert status.flapper is None
        assert status.flopper == self.flopper.address
        assert status.bid == self.sump
        assert status.lot == lot
        assert status.tab is None
        assert status.beg > Wad.from_number(1)
        assert status.guy == self.other_address
        assert status.era > 0
        assert status.end > status.era
        assert status.tic > status.era
        assert status.price == Wad(self.sump / Rad(lot))

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_tick_if_auction_expired_due_to_tau(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once()
        model.terminate.assert_not_called()

        # when
        time_travel_by(self.web3, self.flopper.tau() + 1)
        # and
        simulate_model_output(model=model, price=Wad.from_number(555.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        model.terminate.assert_not_called()
        auction = self.flopper.bids(kick)
        assert round(auction.bid / Rad(auction.lot), 2) == round(Rad.from_number(555.0), 2)

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        model_factory.create_model.assert_called_once()
        self.keeper.check_all_auctions()
        model.terminate.assert_called_once()

    def test_should_terminate_model_if_auction_expired_due_to_ttl_and_somebody_else_won_it(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once()
        model.terminate.assert_not_called()

        # when
        self.dent(kick, self.other_address, Wad.from_number(0.000015), self.sump)
        # and
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once()
        model.terminate.assert_called_once()

        # cleanup
        assert self.flopper.deal(kick).transact()

    def test_should_terminate_model_if_auction_is_dealt(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once()
        model.terminate.assert_not_called()

        # when
        self.dent(kick, self.other_address, Wad.from_number(0.000016), self.sump)
        # and
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        # and
        self.flopper.deal(kick).transact(from_address=self.other_address)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once()
        model.terminate.assert_called_once()

    def test_should_not_instantiate_model_if_auction_is_dealt(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        # and
        self.dent(kick, self.other_address, Wad.from_number(0.000017), self.sump)
        # and
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        # and
        assert self.flopper.deal(kick).transact(from_address=self.other_address)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_not_called()

    def test_should_not_do_anything_if_no_output_from_model(self, kick):
        # given
        previous_block_number = self.web3.eth.blockNumber

        # when
        # [no output from model]
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.web3.eth.blockNumber == previous_block_number

    def test_should_make_initial_bid(self):
        # given
        kick = self.flopper.kicks()
        (model, model_factory) = models(self.keeper, kick)
        vfgt_before = self.mcd.vdgt.balance_of(self.keeper_address)

        # when
        simulate_model_output(model=model, price=Wad.from_number(575.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        auction = self.flopper.bids(kick)
        assert round(auction.bid / Rad(auction.lot), 2) == round(Rad.from_number(575.0), 2)
        vdgt_after = self.mcd.vdgt.balance_of(self.keeper_address)
        assert vfgt_before == vdgt_after

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_bid_even_if_there_is_already_a_bidder(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        vdgt_before = self.mcd.vdgt.balance_of(self.keeper_address)
        # and
        lot = Wad.from_number(0.000016)
        assert self.flopper.dent(kick, lot, self.sump).transact(from_address=self.other_address)
        assert self.flopper.bids(kick).lot == lot

        # when
        simulate_model_output(model=model, price=Wad.from_number(825.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        auction = self.flopper.bids(kick)
        assert auction.lot != lot
        assert round(auction.bid / Rad(auction.lot), 2) == round(Rad.from_number(825.0), 2)
        vdgt_after = self.mcd.vdgt.balance_of(self.keeper_address)
        assert vdgt_before == vdgt_after

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_overbid_itself_if_model_has_updated_the_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(100.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert round(Rad(self.flopper.bids(kick).lot), 2) == round(self.sump / Rad.from_number(100.0), 2)

        # when
        simulate_model_output(model=model, price=Wad.from_number(110.0))
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.lot_implies_price(kick, Wad.from_number(110.0))

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_increase_gas_price_of_pending_transactions_if_model_increases_gas_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(120.0), gas_price=10)
        # and
        self.start_ignoring_transactions()
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        # and
        self.end_ignoring_transactions()
        # and
        simulate_model_output(model=model, price=Wad.from_number(120.0), gas_price=15)
        # and
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.lot_implies_price(kick, Wad.from_number(120.0))
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 15

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_replace_pending_transactions_if_model_raises_bid_and_increases_gas_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(50.0), gas_price=10)
        # and
        self.start_ignoring_transactions()
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        # and
        time.sleep(2)
        # and
        self.end_ignoring_transactions()
        # and
        simulate_model_output(model=model, price=Wad.from_number(60.0), gas_price=15)
        # and
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.lot_implies_price(kick, Wad.from_number(60.0))
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 15

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_replace_pending_transactions_if_model_lowers_bid_and_increases_gas_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(80.0), gas_price=10)
        # and
        self.start_ignoring_transactions()
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        # and
        time.sleep(2)
        # and
        self.end_ignoring_transactions()
        # and
        simulate_model_output(model=model, price=Wad.from_number(70.0), gas_price=15)
        # and
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.lot_implies_price(kick, Wad.from_number(70.0))
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 15

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_not_bid_on_rounding_errors_with_small_amounts(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(1400.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flopper.bids(kick).lot == Wad(self.sump / Rad.from_number(1400.0))

        # when
        tx_count = self.web3.eth.getTransactionCount(self.keeper_address.address)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.web3.eth.getTransactionCount(self.keeper_address.address) == tx_count

    def test_should_deal_when_we_won_the_auction(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(825.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.lot_implies_price(kick, Wad.from_number(825.0))
        vdgt_before = self.mcd.vdgt.balance_of(self.keeper_address)

        # when
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        vdgt_after = self.mcd.vdgt.balance_of(self.keeper_address)
        assert vdgt_before < vdgt_after

    def test_should_not_deal_when_auction_finished_but_somebody_else_won(self, kick):
        # given
        vdgt_before = self.mcd.vdgt.balance_of(self.keeper_address)
        # and
        self.dent(kick, self.other_address, Wad.from_number(0.000015), self.sump)
        assert self.flopper.bids(kick).lot == Wad.from_number(0.000015)

        # when
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        vdgt_after = self.mcd.vdgt.balance_of(self.keeper_address)
        assert vdgt_before == vdgt_after

    def test_should_obey_gas_price_provided_by_the_model(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(800.0), gas_price=175000)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flopper.bids(kick).guy == self.keeper_address
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 175000

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_use_default_gas_price_if_not_provided_by_the_model(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(850.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flopper.bids(kick).guy == self.keeper_address
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == \
               self.default_gas_price

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    def test_should_change_gas_strategy_when_model_output_changes(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        first_bid = Wad.from_number(90)
        simulate_model_output(model=model, price=first_bid, gas_price=2000)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 2000

        # when
        second_bid = Wad.from_number(100)
        simulate_model_output(model=model, price=second_bid)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert round(Rad(self.flopper.bids(kick).lot), 2) == round(self.sump / Rad(second_bid), 2)
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == \
               self.default_gas_price

        # when
        third_bid = Wad.from_number(110)
        new_gas_price = int(self.default_gas_price*1.25)
        simulate_model_output(model=model, price=third_bid, gas_price=new_gas_price)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert round(Rad(self.flopper.bids(kick).lot), 2) == round(self.sump / Rad(third_bid), 2)
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == new_gas_price

        # cleanup
        time_travel_by(self.web3, self.flopper.ttl() + 1)
        assert self.flopper.deal(kick).transact()

    @classmethod
    def cleanup_debt(cls, web3, mcd):
        # Cancel out surplus and debt
        usdv_vow = mcd.vat.usdv(mcd.vow.address)
        assert usdv_vow <= mcd.vow.woe()
        assert mcd.vow.heal(usdv_vow).transact()

    @classmethod
    def teardown_class(cls):
        cls.cleanup_debt(cls.web3, cls.mcd)
        c = cls.mcd.collaterals['VLX-A']
        assert get_collateral_price(c) == Wad.from_number(200.00)
        if not repay_urn(cls.mcd, c, cls.gal_address):
            liquidate_urn(cls.mcd, c, cls.gal_address, cls.keeper_address)
        assert threading.active_count() == 1


class MockFlopper:
    bid = Rad.from_number(50000)
    sump = Wad.from_number(50000)

    def __init__(self):
        self.tau = 259200
        self.ttl = 21600
        self.lot = self.sump
        pass

    def bids(self, id: int):
        return Flopper.Bid(id=id,
                           bid=self.bid,
                           lot=self.lot,
                           guy=Address("0x0000000000000000000000000000000000000000"),
                           tic=0,
                           end=int(datetime.now(tz=timezone.utc).timestamp()) + self.tau)


class TestFlopStrategy:
    def setup_class(cls):
        cls.mcd = mcd(web3())
        cls.strategy = FlopperStrategy(cls.mcd.flopper)
        cls.mock_flopper = MockFlopper()

    def test_price(self, mocker):
        mocker.patch("pymaker.auctions.Flopper.bids", return_value=self.mock_flopper.bids(1))
        mocker.patch("pymaker.auctions.Flopper.dent", return_value="tx goes here")
        model_price = Wad.from_number(190.0)
        (price, tx, bid) = self.strategy.bid(1, model_price)
        assert price == model_price
        assert bid == MockFlopper.bid
        lot1 = MockFlopper.sump / model_price
        Flopper.dent.assert_called_once_with(1, lot1, MockFlopper.bid)

        # When bid price increases, lot should decrease
        model_price = Wad.from_number(200.0)
        (price, tx, bid) = self.strategy.bid(1, model_price)
        lot2 = Flopper.dent.call_args[0][1]
        assert lot2 < lot1
        assert lot2 == MockFlopper.sump / model_price
