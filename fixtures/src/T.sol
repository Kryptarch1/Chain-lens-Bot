// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
// Minimal ERC20 with pluggable rules used as DD regression fixtures. mode: 0 clean, 1 honeypot (no sells),
// 2 delayed (sells blocked after `limit` blocks from launch), 3 cooldown (no sell within `limit` blocks of buy),
// 4 max-sell (sell > limit units reverts), 5 owner drain (owner can zero any balance), 6 owner switch (owner can block sells later)
contract T {
    string public name = "Fixture"; string public symbol = "FIX"; uint8 public decimals = 18;
    uint256 public totalSupply; mapping(address=>uint256) public balanceOf; mapping(address=>mapping(address=>uint256)) public allowance;
    address public owner; address public pair; uint8 public mode; uint256 public limit; uint256 public launchBlock; bool public blocked;
    mapping(address=>uint256) public lastBuy;
    event Transfer(address indexed from, address indexed to, uint256 value); event Approval(address indexed owner, address indexed spender, uint256 value);
    constructor(uint8 m, uint256 l) { owner = msg.sender; mode = m; limit = l; totalSupply = 1e27; balanceOf[msg.sender] = 1e27; emit Transfer(address(0), msg.sender, 1e27); }
    function setPair(address p) external { require(msg.sender == owner); pair = p; launchBlock = block.number; }
    function approve(address s, uint256 v) external returns (bool) { allowance[msg.sender][s] = v; emit Approval(msg.sender, s, v); return true; }
    function transfer(address to, uint256 v) external returns (bool) { _t(msg.sender, to, v); return true; }
    function transferFrom(address f, address to, uint256 v) external returns (bool) { allowance[f][msg.sender] -= v; _t(f, to, v); return true; }
    function drain(address who) external { require(msg.sender == owner); totalSupply -= balanceOf[who]; balanceOf[who] = 0; }
    function setBlocked(bool b) external { require(msg.sender == owner); blocked = b; }
    function _t(address f, address to, uint256 v) internal {
        bool isSell = (to == pair && f != owner);
        bool isBuy = (f == pair);
        if (isBuy) lastBuy[to] = block.number;
        if (isSell) {
            if (mode == 1) revert("no");
            if (mode == 2 && block.number > launchBlock + limit) revert("no");
            if (mode == 3 && block.number < lastBuy[f] + limit) revert("cooldown");
            if (mode == 4 && v > limit) revert("max sell");
            if (mode == 6 && blocked) revert("paused");
        }
        balanceOf[f] -= v; balanceOf[to] += v; emit Transfer(f, to, v);
    }
}
