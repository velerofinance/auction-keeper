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
from pymaker.approval import directly
from pymaker.collateral import Collateral
from pymaker.numeric import Wad, Rad

from auction_keeper.gas import DynamicGasPrice
from auction_keeper.main import AuctionKeeper
from auction_keeper.model import Parameters
from tests.conftest import create_cdp_with_surplus, gal_address, get_node_gas_price, keeper_address, liquidate_urn, \
    mcd, mint_vdgt, models, other_address, our_address, repay_urn, simulate_model_output, web3
from tests.helper import args, kill_other_threads, time_travel_by, wait_for_other_threads, TransactionIgnoringTest


@pytest.fixture(scope="session")
def c(mcd):
    return mcd.collaterals['VLX-C']


@pytest.fixture()
def kick(mcd, c: Collateral, gal_address) -> int:
    create_cdp_with_surplus(mcd, c, gal_address)

    assert mcd.vow.flap().transact(from_address=gal_address)
    kick = mcd.flapper.kicks()
    assert kick > 0

    current_bid = mcd.flapper.bids(kick)
    assert current_bid.lot == mcd.vow.bump()

    return kick


@pytest.mark.timeout(800)
class TestAuctionKeeperFlapper(TransactionIgnoringTest):
    def setup_method(self):
        self.web3 = web3()
        self.our_address = our_address(self.web3)
        self.keeper_address = keeper_address(self.web3)
        self.other_address = other_address(self.web3)
        self.gal_address = gal_address(self.web3)
        self.mcd = mcd(self.web3)
        self.flapper = self.mcd.flapper
        self.flapper.approve(self.mcd.vdgt.address, directly(from_address=self.other_address))

        self.keeper = AuctionKeeper(args=args(f"--eth-from {self.keeper_address} "
                                              f"--type flap "
                                              f"--from-block 1 "
                                              f"--model ./bogus-model.sh"), web3=self.web3)
        self.keeper.approve()

        mint_vdgt(self.mcd.vdgt, self.keeper_address, Wad.from_number(50000))
        mint_vdgt(self.mcd.vdgt, self.other_address, Wad.from_number(50000))

        assert isinstance(self.keeper.gas_price, DynamicGasPrice)
        # Since no args were assigned, gas strategy should return a GeometricGasPrice starting at the node gas price
        self.default_gas_price = get_node_gas_price(self.web3)

    def test_should_detect_flap(self, web3, mcd, c, gal_address, keeper_address):
        # given some VDGT is available to the keeper and a count of flap auctions
        mint_vdgt(mcd.vdgt, keeper_address, Wad.from_number(50000))
        kicks = mcd.flapper.kicks()

        # when surplus is generated
        create_cdp_with_surplus(mcd, c, gal_address)
        self.keeper.check_flap()
        wait_for_other_threads()

        # then ensure another flap auction was kicked off
        kick = mcd.flapper.kicks()
        assert kick == kicks + 1

        # clean up by letting someone else bid and waiting until the auction ends
        auction = self.flapper.bids(kick)
        assert self.flapper.tend(kick, auction.lot, Wad.from_number(30)).transact(from_address=self.other_address)
        time_travel_by(web3, mcd.flapper.ttl() + 1)

    def test_should_start_a_new_model_and_provide_it_with_info_on_auction_kick(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once_with(Parameters(auction_contract=self.keeper.mcd.flapper, id=kick))
        # and
        status = model.send_status.call_args[0][0]
        assert status.id == kick
        assert status.flipper is None
        assert status.flapper == self.flapper.address
        assert status.flopper is None
        assert status.bid == Wad(0)
        assert status.lot == self.mcd.vow.bump()
        assert status.tab is None
        assert status.beg == Wad.from_number(1.05)
        assert status.guy == self.mcd.vow.address
        assert status.era > 0
        assert status.end < status.era + self.flapper.tau() + 1
        assert status.tic == 0
        assert status.price is None

    def test_should_provide_model_with_updated_info_after_our_own_bid(self):
        # given
        kick = self.flapper.kicks()
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert model.send_status.call_count == 1

        # when
        simulate_model_output(model=model, price=Wad.from_number(9))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert model.send_status.call_count > 1
        # and
        status = model.send_status.call_args[0][0]
        assert status.id == kick
        assert status.flipper is None
        assert status.flapper == self.flapper.address
        assert status.flopper is None
        assert status.bid == Wad(self.flapper.bids(kick).lot / Rad.from_number(9))
        assert status.lot == self.mcd.vow.bump()
        assert status.tab is None
        assert status.beg == Wad.from_number(1.05)
        assert status.guy == self.keeper_address
        assert status.era > 0
        assert status.end > status.era
        assert status.tic > status.era
        assert round(status.price, 2) == round(Wad.from_number(9), 2)

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_provide_model_with_updated_info_after_somebody_else_bids(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert model.send_status.call_count == 1

        # when
        auction = self.flapper.bids(kick)
        assert Wad.from_number(40) > auction.bid
        assert self.flapper.tend(kick, auction.lot, Wad.from_number(40)).transact(from_address=self.other_address)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        auction = self.flapper.bids(kick)
        # then
        assert model.send_status.call_count > 1
        # and
        status = model.send_status.call_args[0][0]
        assert status.id == kick
        assert status.flipper is None
        assert status.flapper == self.flapper.address
        assert status.flopper is None
        assert status.bid == Wad.from_number(40)
        assert status.lot == auction.lot
        assert status.tab is None
        assert status.beg == Wad.from_number(1.05)
        assert status.guy == self.other_address
        assert status.era > 0
        assert status.end > status.era
        assert status.tic > status.era
        assert status.price == Wad(auction.lot / Rad(auction.bid))

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

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
        time_travel_by(self.web3, self.flapper.tau() + 1)
        # and
        simulate_model_output(model=model, price=Wad.from_number(9.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        model.terminate.assert_not_called()
        auction = self.flapper.bids(kick)
        assert round(Wad(auction.lot) / auction.bid, 2) == round(Wad.from_number(9.0), 2)

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
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
        auction = self.flapper.bids(kick)
        assert self.flapper.tend(kick, auction.lot, Wad.from_number(40)).transact(from_address=self.other_address)
        # and
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_called_once()
        model.terminate.assert_called_once()

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
        auction = self.flapper.bids(kick)
        assert self.flapper.tend(kick, auction.lot, Wad.from_number(40)).transact(from_address=self.other_address)
        # and
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        # and
        assert self.flapper.deal(kick).transact(from_address=self.other_address)
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
        auction = self.flapper.bids(kick)
        self.flapper.tend(kick, auction.lot, Wad.from_number(40)).transact(from_address=self.other_address)
        # and
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        # and
        self.flapper.deal(kick).transact(from_address=self.other_address)

        # when
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        model_factory.create_model.assert_not_called()

    def test_should_not_do_anything_if_no_output_from_model(self, kick):
        # given
        assert kick
        previous_block_number = self.web3.eth.blockNumber

        # when
        # [no output from model]
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert self.web3.eth.blockNumber == previous_block_number

    def test_should_make_initial_bid(self):
        # given
        kick = self.flapper.kicks()
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(10.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        auction = self.flapper.bids(kick)
        assert round(Wad(auction.lot) / auction.bid, 2) == round(Wad.from_number(10.0), 2)

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_bid_even_if_there_is_already_a_bidder(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        # and
        auction = self.flapper.bids(kick)
        assert self.flapper.tend(kick, auction.lot, Wad.from_number(16)).transact(from_address=self.other_address)
        assert self.flapper.bids(kick).bid == Wad.from_number(16)

        # when
        simulate_model_output(model=model, price=Wad.from_number(0.0000005))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        auction = self.flapper.bids(kick)
        assert round(Wad(auction.lot) / auction.bid, 2) == round(Wad.from_number(0.0000005), 2)

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_outbid_a_zero_bid(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        # and
        auction = self.flapper.bids(kick)
        assert self.flapper.tend(kick, auction.lot, Wad(1)).transact(from_address=self.other_address)
        assert self.flapper.bids(kick).bid == Wad(1)

        # when
        simulate_model_output(model=model, price=Wad.from_number(0.0000006))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        auction = self.flapper.bids(kick)
        assert round(Wad(auction.lot) / auction.bid, 2) == round(Wad.from_number(0.0000006), 2)

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_overbid_itself_if_model_has_updated_the_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        lot = self.flapper.bids(kick).lot

        # when
        first_bid = Wad.from_number(0.0000004)
        simulate_model_output(model=model, price=first_bid)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad(lot / Rad(first_bid))

        # when
        second_bid = Wad.from_number(0.0000003)
        simulate_model_output(model=model, price=second_bid)
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad(lot / Rad(second_bid))

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_increase_gas_price_of_pending_transactions_if_model_increases_gas_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        lot = self.flapper.bids(kick).lot

        # when
        simulate_model_output(model=model, price=Wad.from_number(10.0), gas_price=10)
        # and
        self.start_ignoring_transactions()
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        # and
        self.end_ignoring_transactions()
        # and
        simulate_model_output(model=model, price=Wad.from_number(10.0), gas_price=15)
        # and
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad(lot) / Wad.from_number(10.0)
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 15

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_replace_pending_transactions_if_model_raises_bid_and_increases_gas_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        lot = self.flapper.bids(kick).lot

        # when
        simulate_model_output(model=model, price=Wad.from_number(9.0), gas_price=10)
        # and
        self.start_ignoring_transactions()
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        # and
        self.end_ignoring_transactions()
        # and
        simulate_model_output(model=model, price=Wad.from_number(8.0), gas_price=15)
        # and
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert round(self.flapper.bids(kick).bid, 2) == round(Wad(lot / Rad.from_number(8.0)), 2)
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 15

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_replace_pending_transactions_if_model_lowers_bid_and_increases_gas_price(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        lot = self.flapper.bids(kick).lot

        # when
        simulate_model_output(model=model, price=Wad.from_number(10.0), gas_price=10)
        # and
        self.start_ignoring_transactions()
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        # and
        self.end_ignoring_transactions()
        # and
        simulate_model_output(model=model, price=Wad.from_number(8.0), gas_price=15)
        # and
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad(lot) / Wad.from_number(8.0)
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 15

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_not_bid_on_rounding_errors_with_small_amounts(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        lot = self.flapper.bids(kick).lot

        # when
        price = Wad.from_number(9.0)-Wad(5)
        simulate_model_output(model=model, price=price)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad(lot) / Wad(price)

        # when
        tx_count = self.web3.eth.getTransactionCount(self.keeper_address.address)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.web3.eth.getTransactionCount(self.keeper_address.address) == tx_count

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_deal_when_we_won_the_auction(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(8.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        auction = self.flapper.bids(kick)
        assert auction.bid > Wad(0)
        assert round(Wad(auction.lot) / auction.bid, 2) == round(Wad.from_number(8.0), 2)
        usdv_before = self.mcd.vat.usdv(self.keeper_address)

        # when
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        usdv_after = self.mcd.vat.usdv(self.keeper_address)
        # then
        assert usdv_before < usdv_after

    def test_should_not_deal_when_auction_finished_but_somebody_else_won(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        lot = self.flapper.bids(kick).lot
        # and
        assert self.flapper.tend(kick, lot, Wad.from_number(16)).transact(from_address=self.other_address)
        assert self.flapper.bids(kick).bid == Wad.from_number(16)

        # when
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        # and
        self.keeper.check_all_auctions()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad.from_number(16)

    def test_should_obey_gas_price_provided_by_the_model(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(10.0), gas_price=175000)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 175000

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_use_default_gas_price_if_not_provided_by_the_model(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)

        # when
        simulate_model_output(model=model, price=Wad.from_number(10.0))
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        auction = self.flapper.bids(kick)
        assert auction.guy == self.keeper_address
        assert auction.bid > Wad(0)
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == \
               self.default_gas_price

        print(f"tx gas price is {self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice}, "
              f"web3.eth.gasPrice is {self.web3.eth.gasPrice}")

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    def test_should_change_gas_strategy_when_model_output_changes(self, kick):
        # given
        (model, model_factory) = models(self.keeper, kick)
        lot = self.flapper.bids(kick).lot

        # when
        first_bid = Wad.from_number(0.0000009)
        simulate_model_output(model=model, price=first_bid, gas_price=2000)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == 2000

        # when
        second_bid = Wad.from_number(0.0000006)
        simulate_model_output(model=model, price=second_bid)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad(lot / Rad(second_bid))
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == \
               self.default_gas_price

        # when
        third_bid = Wad.from_number(0.0000003)
        new_gas_price = int(self.default_gas_price*1.25)
        simulate_model_output(model=model, price=third_bid, gas_price=new_gas_price)
        # and
        self.keeper.check_all_auctions()
        self.keeper.check_for_bids()
        wait_for_other_threads()
        # then
        assert self.flapper.bids(kick).bid == Wad(lot / Rad(third_bid))
        assert self.web3.eth.getBlock('latest', full_transactions=True).transactions[0].gasPrice == new_gas_price

        # cleanup
        time_travel_by(self.web3, self.flapper.ttl() + 1)
        assert self.flapper.deal(kick).transact()

    @classmethod
    def teardown_class(cls):
        cls.mcd = mcd(web3())
        if not repay_urn(cls.mcd, c(cls.mcd), gal_address(web3())):
            liquidate_urn(cls.mcd, c(cls.mcd), gal_address(web3()), keeper_address(web3()))
        kill_other_threads()
