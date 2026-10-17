// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

// Router-style v2 sell used ONLY inside eth_simulateV1 (placed at a fixed address by state override, nothing is
// deployed or broadcast). Like a real router: pulls the seller's tokens into the pair (transferFrom), re-reads the
// reserves AFTER the transfer (tax tokens move reserves inside transfer), then swaps the quote token to the seller.

interface IERC20h { function transferFrom(address f, address t, uint256 a) external returns (bool); function balanceOf(address a) external view returns (uint256); }
interface IPairh { function getReserves() external view returns (uint112, uint112, uint32); function token0() external view returns (address);
                   function swap(uint256 a0, uint256 a1, address to, bytes calldata data) external; }

contract SellHelper {
    function sellV2(address token, address pair, uint256 amount) external returns (uint256 out) {
        require(IERC20h(token).transferFrom(msg.sender, pair, amount), "transferFrom returned false");
        (uint112 r0, uint112 r1, ) = IPairh(pair).getReserves();
        bool t0 = IPairh(pair).token0() == token;
        (uint256 rT, uint256 rQ) = t0 ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
        uint256 bal = IERC20h(token).balanceOf(pair);
        require(bal > rT, "pair received nothing");
        uint256 ain = bal - rT;
        out = ain * 997 * rQ / (rT * 1000 + ain * 997) * 97 / 100; // tolerate 0.25-0.3% fee tiers and rounding
        require(out > 0, "pool too shallow: swap output would be 0");
        IPairh(pair).swap(t0 ? 0 : out, t0 ? out : 0, msg.sender, "");
    }
}
