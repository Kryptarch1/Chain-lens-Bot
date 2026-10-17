// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

// Test-only malicious v4 hook + plain token, deployed on a private fork to prove the hook sim catches:
//   mode 0: 3% fee to treasury (clean-ish)      mode 1: sells always revert (honeypot)
//   mode 2: sells revert 30 min after start()   any mode: owner can setFee (no cap) / setBlocked
// The runtime code is copied (anvil_setCode) to an address whose low bits = afterSwap | afterSwapReturnDelta.

import {PoolKey, SwapParams} from "./V4Helper.sol";

interface IPMt { function take(address currency, address to, uint256 amount) external; }

contract HT {
    string public name = "HookTest"; string public symbol = "HT"; uint8 public decimals = 18;
    uint256 public totalSupply; mapping(address => uint256) public balanceOf; mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    constructor(address to, uint256 amt) { totalSupply = amt; balanceOf[to] = amt; emit Transfer(address(0), to, amt); }
    function approve(address s, uint256 a) external returns (bool) { allowance[msg.sender][s] = a; emit Approval(msg.sender, s, a); return true; }
    function transfer(address t, uint256 a) external returns (bool) { _t(msg.sender, t, a); return true; }
    function transferFrom(address f, address t, uint256 a) external returns (bool) {
        if (allowance[f][msg.sender] != type(uint256).max) allowance[f][msg.sender] -= a;
        _t(f, t, a); return true;
    }
    function _t(address f, address t, uint256 a) internal { balanceOf[f] -= a; balanceOf[t] += a; emit Transfer(f, t, a); }
}

contract FeeHook {
    address public immutable pm;
    address public immutable owner;
    address public immutable treasury;
    address public immutable token;
    uint256 public mode;      // storage: set after anvil_setCode via owner calls
    uint256 public feeBps;
    uint256 public start;
    bool public blocked;

    constructor(address _pm, address _owner, address _treasury, address _token) { pm = _pm; owner = _owner; treasury = _treasury; token = _token; }

    function setup(uint256 m, uint256 fee) external { require(msg.sender == owner); mode = m; feeBps = fee; start = block.timestamp; }
    function setFee(uint256 fee) external { require(msg.sender == owner, "not owner"); feeBps = fee; }
    function setBlocked(bool b) external { require(msg.sender == owner, "not owner"); blocked = b; }

    function afterSwap(address, PoolKey calldata key, SwapParams calldata p, int256 delta, bytes calldata) external returns (bytes4, int128) {
        require(msg.sender == pm, "only pm");
        bool tokenIs0 = key.currency0 == token;
        bool isSell = (p.zeroForOne == tokenIs0); // paying the token in = selling
        if (isSell) {
            require(!blocked, "blocked");
            require(mode != 1, "sells disabled");
            require(!(mode == 2 && block.timestamp > start + 1800), "trading window closed");
        }
        int128 out = p.zeroForOne ? int128(delta) : int128(delta >> 128); // unspecified (output) side for exact-input
        if (out <= 0 || feeBps == 0) return (FeeHook.afterSwap.selector, 0);
        uint256 fee = uint256(uint128(out)) * feeBps / 10000;
        address cur = p.zeroForOne ? key.currency1 : key.currency0;
        IPMt(pm).take(cur, treasury, fee);
        return (FeeHook.afterSwap.selector, int128(int256(fee)));
    }
}
