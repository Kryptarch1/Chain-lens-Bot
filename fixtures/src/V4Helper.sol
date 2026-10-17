// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

// Minimal Uniswap v4 router used ONLY on private forks: real swaps / liquidity changes through the
// PoolManager, so the pool's hook runs exactly as it would for a real trade. Funds are pulled from the
// caller (transferFrom caller -> PoolManager, like Permit2 routers) and paid back to the caller.

struct PoolKey { address currency0; address currency1; uint24 fee; int24 tickSpacing; address hooks; }
struct SwapParams { bool zeroForOne; int256 amountSpecified; uint160 sqrtPriceLimitX96; }
struct ModifyLiquidityParams { int24 tickLower; int24 tickUpper; int256 liquidityDelta; bytes32 salt; }

interface IPM {
    function unlock(bytes calldata data) external returns (bytes memory);
    function swap(PoolKey memory key, SwapParams memory params, bytes calldata hookData) external returns (int256);
    function modifyLiquidity(PoolKey memory key, ModifyLiquidityParams memory params, bytes calldata hookData) external returns (int256, int256);
    function sync(address currency) external;
    function settle() external payable returns (uint256);
    function take(address currency, address to, uint256 amount) external;
}

interface IERC20 { function transferFrom(address f, address t, uint256 a) external returns (bool); }

contract V4Helper {
    IPM public immutable pm;
    uint160 constant MIN_SQRT = 4295128740;
    uint160 constant MAX_SQRT = 1461446703485210103287273052203988822378723970341;
    event Deltas(int128 amount0, int128 amount1);

    constructor(address _pm) { pm = IPM(_pm); }

    /// exact-input swap of `amountIn`; returns the caller's deltas (negative = paid, positive = received)
    function swap(PoolKey calldata key, bool zeroForOne, uint256 amountIn) external payable returns (int128, int128) {
        bytes memory r = pm.unlock(abi.encode(uint8(0), msg.sender, key, zeroForOne, int256(amountIn), int24(0), int24(0), bytes32(0)));
        return _done(r);
    }

    /// add (liquidity > 0) or remove (liquidity < 0) this helper's own position
    function liquidity(PoolKey calldata key, int24 tickLower, int24 tickUpper, int256 liq, bytes32 salt) external payable returns (int128, int128) {
        bytes memory r = pm.unlock(abi.encode(uint8(1), msg.sender, key, false, liq, tickLower, tickUpper, salt));
        return _done(r);
    }

    function _done(bytes memory r) internal returns (int128 a0, int128 a1) {
        (a0, a1) = abi.decode(r, (int128, int128));
        emit Deltas(a0, a1);
        if (address(this).balance > 0) payable(msg.sender).transfer(address(this).balance); // refund unused native
    }

    function unlockCallback(bytes calldata data) external returns (bytes memory) {
        require(msg.sender == address(pm), "only pm");
        (uint8 op, address payer, PoolKey memory key, bool z, int256 amt, int24 tl, int24 tu, bytes32 salt) =
            abi.decode(data, (uint8, address, PoolKey, bool, int256, int24, int24, bytes32));
        int256 d;
        if (op == 0) {
            d = pm.swap(key, SwapParams(z, -amt, z ? MIN_SQRT : MAX_SQRT), "");
        } else {
            (d, ) = pm.modifyLiquidity(key, ModifyLiquidityParams(tl, tu, amt, salt), "");
        }
        int128 a0 = int128(d >> 128);
        int128 a1 = int128(d);
        _settle(key.currency0, a0, payer);
        _settle(key.currency1, a1, payer);
        return abi.encode(a0, a1);
    }

    function _settle(address c, int128 a, address payer) internal {
        if (a < 0) {
            uint256 owe = uint256(uint128(-a));
            if (c == address(0)) {
                pm.settle{value: owe}();
            } else {
                pm.sync(c);
                require(IERC20(c).transferFrom(payer, address(pm), owe), "transferFrom failed");
                pm.settle();
            }
        } else if (a > 0) {
            pm.take(c, payer, uint256(uint128(a)));
        }
    }

    receive() external payable {}
}
