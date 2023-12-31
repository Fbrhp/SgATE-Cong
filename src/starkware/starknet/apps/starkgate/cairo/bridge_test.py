import asyncio
from typing import Dict, List, Tuple

import pytest
import pytest_asyncio

from starkware.starknet.apps.starkgate.cairo.contracts import bridge_contract_class
from starkware.starknet.apps.starkgate.conftest import str_to_felt
from starkware.starknet.business_logic.execution.objects import Event
from starkware.starknet.public.abi import get_selector_from_name
from starkware.starknet.solidity.starknet_test_utils import Uint256
from starkware.starknet.std_contracts.ERC20.contracts import erc20_contract_class
from starkware.starknet.std_contracts.upgradability_proxy.contracts import proxy_contract_class
from starkware.starknet.std_contracts.upgradability_proxy.test_utils import advance_time
from starkware.starknet.testing.contract import DeclaredClass, StarknetContract, StarknetState
from starkware.starknet.testing.starknet import Starknet
from starkware.starkware_utils.error_handling import StarkException

ETH_ADDRESS_BOUND = 2**160
GOVERNOR_ADDRESS = str_to_felt("GOVERNOR")
L1_BRIDGE_ADDRESS = 42
L1_ACCOUNT = 1
L1_BRIDGE_SET_EVENT_IDENTIFIER = "l1_bridge_set"
L2_TOKEN_SET_EVENT_IDENTIFIER = "l2_token_set"
WITHDRAW_INITIATED_EVENT_IDENTIFIER = "withdraw_initiated"
DEPOSIT_HANDLED_EVENT_IDENTIFIER = "deposit_handled"
BRIDGE_CONTRACT_IDENTITY = "STARKGATE"
BRIDGE_CONTRACT_VERSION = 1


INITIAL_BALANCES = {1: 13, 2: 10}
UNFUNDED_ACCOUNT = 3
INITIAL_TOTAL_SUPPLY = sum(INITIAL_BALANCES.values())
FUNDED_ACCOUNT = next(iter(INITIAL_BALANCES))

# 0 < BURN_AMOUNT < MINT_AMOUNT.
MINT_AMOUNT = 15
BURN_AMOUNT = MINT_AMOUNT - 1
UPGRADE_DELAY = 0


def copy_contract(contract: StarknetContract, state: StarknetState) -> StarknetContract:
    assert contract.abi is not None, "Missing ABI."
    return StarknetContract(
        state=state,
        abi=contract.abi,
        contract_address=contract.contract_address,
        deploy_call_info=contract.deploy_call_info,
    )


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def session_starknet() -> Starknet:
    starknet = await Starknet.empty()
    # We want to start with a non-zero block/time (this would fail tests).
    advance_time(starknet=starknet, block_time_diff=1, block_num_diff=1)
    return starknet


@pytest_asyncio.fixture(scope="session")
async def session_proxy_contract(session_starknet: Starknet) -> StarknetContract:
    proxy = await session_starknet.deploy(
        constructor_calldata=[UPGRADE_DELAY], contract_class=proxy_contract_class
    )
    await proxy.init_governance().execute(caller_address=GOVERNOR_ADDRESS)
    return proxy


@pytest_asyncio.fixture(scope="session")
async def declared_bridge_impl(session_starknet: Starknet) -> DeclaredClass:
    return await session_starknet.declare(contract_class=bridge_contract_class)


@pytest_asyncio.fixture(scope="session")
async def session_token_contract(
    session_starknet: Starknet,
    token_name: int,
    token_symbol: int,
    token_decimals: int,
    session_proxy_contract: StarknetContract,
) -> StarknetContract:
    token_proxy = await session_starknet.deploy(
        constructor_calldata=[UPGRADE_DELAY], contract_class=proxy_contract_class
    )
    await token_proxy.init_governance().execute(caller_address=GOVERNOR_ADDRESS)
    l2_bridge_address = session_proxy_contract.contract_address
    declared_token_impl = await session_starknet.declare(contract_class=erc20_contract_class)
    NOT_FINAL = False
    NO_EIC = 0
    proxy_func_params = [
        declared_token_impl.class_hash,
        NO_EIC,
        [
            token_name,
            token_symbol,
            token_decimals,
            l2_bridge_address,
        ],
        NOT_FINAL,
    ]
    # Set a first implementation on the proxy.
    await token_proxy.add_implementation(*proxy_func_params).execute(
        caller_address=GOVERNOR_ADDRESS
    )
    await token_proxy.upgrade_to(*proxy_func_params).execute(caller_address=GOVERNOR_ADDRESS)
    wrapped_token = token_proxy.replace_abi(impl_contract_abi=declared_token_impl.abi)

    # Initial balance setup.
    for account in INITIAL_BALANCES:
        await wrapped_token.permissionedMint(
            recipient=account, amount=Uint256(INITIAL_BALANCES[account]).uint256()
        ).execute(caller_address=l2_bridge_address)
    return wrapped_token


@pytest.fixture
def starknet(
    session_starknet: Starknet, session_uninitialized_bridge_contract: StarknetContract
) -> Starknet:
    # Bridge contract is passed for order enforcement. Enforces state clone only post proxy wiring.
    return session_starknet.copy()


@pytest.fixture(scope="session")
async def session_uninitialized_bridge_contract(
    session_proxy_contract: StarknetContract,
    declared_bridge_impl: DeclaredClass,
) -> StarknetContract:
    NOT_FINAL = False
    NO_EIC = 0
    proxy_func_params = [
        declared_bridge_impl.class_hash,
        NO_EIC,
        [GOVERNOR_ADDRESS],
        NOT_FINAL,
    ]
    # Set a first implementation on the proxy.
    await session_proxy_contract.add_implementation(*proxy_func_params).execute(
        caller_address=GOVERNOR_ADDRESS
    )
    await session_proxy_contract.upgrade_to(*proxy_func_params).execute(
        caller_address=GOVERNOR_ADDRESS
    )
    return session_proxy_contract.replace_abi(impl_contract_abi=declared_bridge_impl.abi)


@pytest.fixture(scope="session")
async def session_initialized_bridge_contract(
    session_starknet: Starknet,
    session_uninitialized_bridge_contract: StarknetContract,
    session_token_contract: StarknetContract,
) -> StarknetContract:
    wrapped_bridge = session_uninitialized_bridge_contract

    # Set L1 bridge address on the bridge.
    await wrapped_bridge.set_l1_bridge(l1_bridge_address=L1_BRIDGE_ADDRESS).execute(
        caller_address=GOVERNOR_ADDRESS
    )
    assert (await wrapped_bridge.get_l1_bridge().call()).result[0] == L1_BRIDGE_ADDRESS

    # Verify emission of respective event.
    expected_event = Event(
        from_address=wrapped_bridge.contract_address,
        keys=[get_selector_from_name(L1_BRIDGE_SET_EVENT_IDENTIFIER)],
        data=[L1_BRIDGE_ADDRESS],
    )
    assert expected_event == session_starknet.state.events[-1]

    # Set L2 token address on the bridge.
    l2_token_address = session_token_contract.contract_address
    await wrapped_bridge.set_l2_token(l2_token_address=l2_token_address).execute(
        caller_address=GOVERNOR_ADDRESS
    )

    # Verify emission of respective event.
    expected_event = Event(
        from_address=wrapped_bridge.contract_address,
        keys=[get_selector_from_name(L2_TOKEN_SET_EVENT_IDENTIFIER)],
        data=[l2_token_address],
    )
    assert expected_event == session_starknet.state.events[-1]
    assert (await wrapped_bridge.get_l2_token().call()).result[0] == l2_token_address
    return wrapped_bridge


@pytest.fixture
def bridge_contract(
    starknet: Starknet,
    session_initialized_bridge_contract: StarknetContract,
) -> StarknetContract:
    return copy_contract(contract=session_initialized_bridge_contract, state=starknet.state)


@pytest.fixture
async def uninitialized_bridge_contract(
    starknet: Starknet,
    session_uninitialized_bridge_contract: StarknetContract,
) -> StarknetContract:
    return copy_contract(contract=session_uninitialized_bridge_contract, state=starknet.state)


@pytest.fixture
def token_contract(
    starknet: Starknet, session_token_contract: StarknetContract
) -> StarknetContract:
    return copy_contract(contract=session_token_contract, state=starknet.state)


@pytest.mark.asyncio
async def test_uninitialized_bridge_getters(uninitialized_bridge_contract: StarknetContract):
    # Governor is set in addImplementation, so it should be valid.
    assert (await uninitialized_bridge_contract.get_governor().call()).result[0] == GOVERNOR_ADDRESS
    # Token and L1 bridge addresses should not be set.
    assert (await uninitialized_bridge_contract.get_l1_bridge().call()).result[0] == 0
    assert (await uninitialized_bridge_contract.get_l2_token().call()).result[0] == 0


@pytest.mark.asyncio
async def test_handle_deposit_uninitialized_bridge(
    starknet: Starknet,
    uninitialized_bridge_contract: StarknetContract,
):
    async def invoke_handle_deposit():
        await starknet.send_message_to_l2(
            from_address=L1_BRIDGE_ADDRESS,
            to_address=uninitialized_bridge_contract.contract_address,
            selector=get_selector_from_name("handle_deposit"),
            payload=[FUNDED_ACCOUNT, Uint256(MINT_AMOUNT).low, Uint256(MINT_AMOUNT).high],
        )

    with pytest.raises(StarkException, match=r"UNINITIALIZED_L1_BRIDGE_ADDRESS"):
        await invoke_handle_deposit()

    # Set L1 bridge address to continue to the next error.
    await uninitialized_bridge_contract.set_l1_bridge(l1_bridge_address=L1_BRIDGE_ADDRESS).execute(
        caller_address=GOVERNOR_ADDRESS
    )

    with pytest.raises(StarkException, match=r"UNINITIALIZED_TOKEN"):
        await invoke_handle_deposit()


@pytest.mark.asyncio
async def test_initiate_withdraw_uninitialized_bridge(
    uninitialized_bridge_contract: StarknetContract,
):
    async def invoke_initiate_withdraw():
        await uninitialized_bridge_contract.initiate_withdraw(
            l1_recipient=FUNDED_ACCOUNT,
            amount=Uint256(INITIAL_BALANCES[FUNDED_ACCOUNT]).uint256(),
        ).call(caller_address=FUNDED_ACCOUNT)

    with pytest.raises(StarkException, match=r"UNINITIALIZED_L1_BRIDGE_ADDRESS"):
        await invoke_initiate_withdraw()

    # Set L1 bridge address to continue to the next error.
    await uninitialized_bridge_contract.set_l1_bridge(l1_bridge_address=L1_BRIDGE_ADDRESS).execute(
        caller_address=GOVERNOR_ADDRESS
    )

    with pytest.raises(StarkException, match=r"UNINITIALIZED_TOKEN"):
        await invoke_initiate_withdraw()


@pytest.mark.asyncio
async def test_bridge_wrapped_properly(
    declared_bridge_impl: StarknetContract,
    session_initialized_bridge_contract: StarknetContract,
    bridge_contract: StarknetContract,
    session_proxy_contract: StarknetContract,
):
    session_bridge_contract = session_initialized_bridge_contract
    bridge_class_hash = (await session_proxy_contract.implementation().call()).result[0]
    assert declared_bridge_impl.class_hash == bridge_class_hash
    assert session_bridge_contract.contract_address == bridge_contract.contract_address
    assert session_bridge_contract.state is not bridge_contract.state
    assert session_bridge_contract.contract_address == session_proxy_contract.contract_address
    assert (await bridge_contract.initialized().call()).result[0] == True


@pytest.mark.asyncio
async def test_get_governor(bridge_contract: StarknetContract):
    execution_info = await bridge_contract.get_governor().call()
    assert execution_info.result[0] == GOVERNOR_ADDRESS


@pytest.mark.asyncio
async def test_getter_setter_l1_bridge(bridge_contract: StarknetContract):
    # Test that L1 bridge address was set correctly.
    execution_info = await bridge_contract.get_l1_bridge().call()
    assert execution_info.result[0] == L1_BRIDGE_ADDRESS
    # Fail to call set_l1_bridge from a non-governor address.
    with pytest.raises(StarkException, match=r"GOVERNOR_ONLY"):
        await bridge_contract.set_l1_bridge(l1_bridge_address=L1_BRIDGE_ADDRESS).call(
            caller_address=L1_ACCOUNT
        )
    # Fail to override an already set L1 bridge address.
    with pytest.raises(StarkException, match=r"BRIDGE_ALREADY_INITIALIZED"):
        await bridge_contract.set_l1_bridge(l1_bridge_address=L1_BRIDGE_ADDRESS).call(
            caller_address=GOVERNOR_ADDRESS
        )


@pytest.mark.asyncio
async def test_getter_setter_l2_token(
    bridge_contract: StarknetContract, token_contract: StarknetContract
):
    # Test that L2 token address was set correctly.
    l2_token_address = token_contract.contract_address
    execution_info = await bridge_contract.get_l2_token().call()
    assert execution_info.result[0] == l2_token_address
    # Fail to call set_l2_token from a non-governor address.
    with pytest.raises(StarkException, match=r"GOVERNOR_ONLY"):
        await bridge_contract.set_l2_token(l2_token_address=l2_token_address).call(
            caller_address=L1_ACCOUNT
        )
    # Fail to override an already set L2 token address.
    with pytest.raises(StarkException, match=r"L2_TOKEN_ALREADY_INITIALIZED"):
        await bridge_contract.set_l2_token(l2_token_address=l2_token_address).call(
            caller_address=GOVERNOR_ADDRESS
        )


@pytest.mark.asyncio
async def test_get_identity(bridge_contract: StarknetContract):
    execution_info = await bridge_contract.get_identity().call()
    assert execution_info.result[0] == str_to_felt(BRIDGE_CONTRACT_IDENTITY)


@pytest.mark.asyncio
async def test_get_version(bridge_contract: StarknetContract):
    execution_info = await bridge_contract.get_version().call()
    assert execution_info.result[0] == BRIDGE_CONTRACT_VERSION


@pytest.mark.asyncio
async def test_handle_deposit_wrong_l1_address(
    starknet: Starknet,
    bridge_contract: StarknetContract,
):
    with pytest.raises(StarkException, match=r"assert from_address = l1_bridge_"):
        await starknet.send_message_to_l2(
            from_address=L1_BRIDGE_ADDRESS + 1,
            to_address=bridge_contract.contract_address,
            selector=get_selector_from_name("handle_deposit"),
            payload=[FUNDED_ACCOUNT, Uint256(MINT_AMOUNT).low, Uint256(MINT_AMOUNT).high],
        )


@pytest.mark.asyncio
async def test_handle_deposit_zero_account(
    starknet: Starknet,
    bridge_contract: StarknetContract,
):
    with pytest.raises(StarkException, match=r"assert_not_zero\(account\)"):
        await starknet.send_message_to_l2(
            from_address=L1_BRIDGE_ADDRESS,
            to_address=bridge_contract.contract_address,
            selector=get_selector_from_name("handle_deposit"),
            payload=[0, Uint256(MINT_AMOUNT).low, Uint256(MINT_AMOUNT).high],
        )


@pytest.mark.asyncio
async def test_handle_deposit_total_supply_out_of_range(
    starknet: Starknet,
    bridge_contract: StarknetContract,
):
    amount = Uint256(2**256 - INITIAL_TOTAL_SUPPLY)
    with pytest.raises(StarkException, match=r"assert \(is_overflow\) = 0"):
        await starknet.send_message_to_l2(
            from_address=L1_BRIDGE_ADDRESS,
            to_address=bridge_contract.contract_address,
            selector=get_selector_from_name("handle_deposit"),
            payload=[UNFUNDED_ACCOUNT, amount.low, amount.high],
        )


@pytest.mark.asyncio
async def test_handle_deposit_overflow(
    starknet: Starknet,
    bridge_contract: StarknetContract,
):
    amount = Uint256(2**256 - INITIAL_BALANCES[FUNDED_ACCOUNT])
    with pytest.raises(StarkException, match=r"OVERFLOW"):
        await starknet.send_message_to_l2(
            from_address=L1_BRIDGE_ADDRESS,
            to_address=bridge_contract.contract_address,
            selector=get_selector_from_name("handle_deposit"),
            payload=[FUNDED_ACCOUNT, amount.low, amount.high],
        )


@pytest.mark.parametrize(
    "deposits_accounts_and_amounts",
    [
        pytest.param([(UNFUNDED_ACCOUNT, MINT_AMOUNT)], id="simple_deposit"),
        pytest.param(
            [(FUNDED_ACCOUNT, MINT_AMOUNT), (FUNDED_ACCOUNT, MINT_AMOUNT * 2)],
            id="two_deposits",
        ),
    ],
)
@pytest.mark.asyncio
async def test_handle_deposit_happy_flow(
    starknet: Starknet,
    token_contract: StarknetContract,
    bridge_contract: StarknetContract,
    deposits_accounts_and_amounts: List[Tuple[int, int]],
):
    await perform_and_validate_mock_deposits(
        starknet=starknet,
        token_contract=token_contract,
        bridge_contract=bridge_contract,
        deposits_accounts_and_amounts=deposits_accounts_and_amounts,
    )


async def perform_and_validate_mock_deposits(
    starknet: Starknet,
    token_contract: StarknetContract,
    bridge_contract: StarknetContract,
    deposits_accounts_and_amounts: List[Tuple[int, int]],
):
    """
    Perform and validate the deposits specified in deposits_accounts_and_amounts consecutively.
    """
    accounts = {
        deposit_account_and_amount[0]
        for deposit_account_and_amount in deposits_accounts_and_amounts
    }
    account_balances = {account: INITIAL_BALANCES.get(account, 0) for account in accounts}
    total_supply = INITIAL_TOTAL_SUPPLY

    for account, deposit_amount in deposits_accounts_and_amounts:

        deposit_payload = [account, Uint256(deposit_amount).low, Uint256(deposit_amount).high]
        account_balances[account] += deposit_amount
        total_supply += deposit_amount
        expected_event = Event(
            from_address=bridge_contract.contract_address,
            keys=[get_selector_from_name(DEPOSIT_HANDLED_EVENT_IDENTIFIER)],
            data=deposit_payload,
        )

        await starknet.send_message_to_l2(
            from_address=L1_BRIDGE_ADDRESS,
            to_address=bridge_contract.contract_address,
            selector=get_selector_from_name("handle_deposit"),
            payload=deposit_payload,
        )

        # The deposit_handled event should be the last event emitted.
        assert expected_event == starknet.state.events[-1]

        execution_info = await token_contract.balanceOf(account=account).call()
        assert execution_info.result[0] == Uint256(account_balances[account]).uint256()
        execution_info = await token_contract.totalSupply().call()
        assert execution_info.result[0] == Uint256(total_supply).uint256()


@pytest.mark.parametrize("l1_recipient", [ETH_ADDRESS_BOUND, 0])
@pytest.mark.asyncio
async def test_initiate_withdraw_invalid_l1_recipient(
    bridge_contract: StarknetContract,
    l1_recipient: int,
):
    with pytest.raises(StarkException, match=r"assert_eth_address_range\(l1_recipient\)"):
        await bridge_contract.initiate_withdraw(
            l1_recipient=l1_recipient,
            amount=Uint256(INITIAL_BALANCES[FUNDED_ACCOUNT]).uint256(),
        ).call(caller_address=FUNDED_ACCOUNT)


@pytest.mark.asyncio
async def test_initiate_withdraw_to_zero_account(bridge_contract: StarknetContract):
    with pytest.raises(StarkException, match=r"assert_eth_address_range\(l1_recipient\)"):
        await bridge_contract.initiate_withdraw(
            l1_recipient=0, amount=Uint256(BURN_AMOUNT).uint256()
        ).call(caller_address=FUNDED_ACCOUNT)


@pytest.mark.asyncio
async def test_initiate_withdraw_amount_bigger_than_balance(bridge_contract: StarknetContract):
    with pytest.raises(StarkException, match=r"INSUFFICIENT_FUNDS"):
        await bridge_contract.initiate_withdraw(
            l1_recipient=L1_ACCOUNT,
            amount=Uint256(INITIAL_BALANCES[FUNDED_ACCOUNT] + 1).uint256(),
        ).call(caller_address=FUNDED_ACCOUNT)


def get_account_balances(deposits_accounts_and_amounts: List[Tuple[int, int]]) -> Dict[int, int]:
    """
    Get a mapping from account id to the amount the account owns. This function assumes the deposits
    in deposits_accounts_and_amounts are the only deposits and that no withdrawals were performed.
    """
    account_to_amount: Dict[int, int] = {}

    for account, initial_balance in INITIAL_BALANCES.items():
        account_to_amount[account] = initial_balance

    for account, deposit_amount in deposits_accounts_and_amounts:
        account_to_amount[account] = account_to_amount.get(account, 0) + deposit_amount

    return account_to_amount


@pytest.mark.parametrize(
    "deposits_accounts_and_amounts,withdrawals_accounts_and_amounts",
    [
        pytest.param([], [(FUNDED_ACCOUNT, 1), (FUNDED_ACCOUNT, 2)], id="two_withdrawals"),
        pytest.param(
            [(FUNDED_ACCOUNT, MINT_AMOUNT)],
            [(FUNDED_ACCOUNT, BURN_AMOUNT)],
            id="deposit_and_withdrawal",
        ),
    ],
)
@pytest.mark.asyncio
async def test_deposits_and_withdrawals_happy_flow(
    starknet: Starknet,
    token_contract: StarknetContract,
    bridge_contract: StarknetContract,
    deposits_accounts_and_amounts: List[Tuple[int, int]],
    withdrawals_accounts_and_amounts: List[Tuple[int, int]],
):
    await perform_and_validate_mock_deposits_and_withdrawals_happy_flow(
        starknet=starknet,
        token_contract=token_contract,
        bridge_contract=bridge_contract,
        deposits_accounts_and_amounts=deposits_accounts_and_amounts,
        withdrawals_accounts_and_amounts=withdrawals_accounts_and_amounts,
    )


@pytest.mark.asyncio
async def test_withdraw_zero_amount(bridge_contract: StarknetContract):
    with pytest.raises(StarkException, match=r"ZERO_WITHDRAWAL"):
        await bridge_contract.initiate_withdraw(
            l1_recipient=L1_ACCOUNT, amount=Uint256(0).uint256()
        ).call(caller_address=FUNDED_ACCOUNT)


async def perform_and_validate_mock_deposits_and_withdrawals_happy_flow(
    starknet: Starknet,
    token_contract: StarknetContract,
    bridge_contract: StarknetContract,
    deposits_accounts_and_amounts: List[Tuple[int, int]],
    withdrawals_accounts_and_amounts: List[Tuple[int, int]],
):
    await perform_and_validate_mock_deposits(
        starknet=starknet,
        token_contract=token_contract,
        bridge_contract=bridge_contract,
        deposits_accounts_and_amounts=deposits_accounts_and_amounts,
    )

    account_to_amount = get_account_balances(
        deposits_accounts_and_amounts=deposits_accounts_and_amounts
    )

    await perform_and_validate_mock_withdrawals(
        starknet=starknet,
        token_contract=token_contract,
        bridge_contract=bridge_contract,
        withdrawals_accounts_and_amounts=withdrawals_accounts_and_amounts,
        account_to_amount=account_to_amount,
    )


async def perform_and_validate_mock_withdrawals(
    starknet: Starknet,
    token_contract: StarknetContract,
    bridge_contract: StarknetContract,
    withdrawals_accounts_and_amounts: List[Tuple[int, int]],
    account_to_amount: Dict[int, int],
):
    """
    Perform and validate the withdrawals specified in
    withdrawals_accounts_and_amounts consecutively.
    account_to_amount specifies the balances of all relevant accounts and will be updated after
    every withdrawal.
    """
    for account, withdrawal_amount in withdrawals_accounts_and_amounts:

        account_to_amount[account] -= withdrawal_amount
        expected_event = Event(
            from_address=bridge_contract.contract_address,
            keys=[get_selector_from_name(WITHDRAW_INITIATED_EVENT_IDENTIFIER)],
            data=[
                L1_ACCOUNT,
                Uint256(withdrawal_amount).low,
                Uint256(withdrawal_amount).high,
                account,
            ],
        )

        await bridge_contract.initiate_withdraw(
            l1_recipient=L1_ACCOUNT, amount=Uint256(withdrawal_amount).uint256()
        ).execute(caller_address=account)

        # The withdraw_initiated event should be the last event emitted.
        assert expected_event == starknet.state.events[-1]

        execution_info = await token_contract.balanceOf(account=account).call()
        assert execution_info.result[0] == Uint256(account_to_amount[account]).uint256()
        execution_info = await token_contract.totalSupply().call()
        assert execution_info.result[0] == Uint256(sum(account_to_amount.values())).uint256()
