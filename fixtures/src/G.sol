// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
// Access-check shapes the analyser must classify correctly (tests/gates.py).
contract G {
    address public owner = msg.sender;
    address public pool;
    uint256 public fee;
    mapping(address => bool) public pref;
    error NotOwner();
    function setFeeRequire(uint256 f) external { require(msg.sender == owner, "owner"); fee = f; }   // gate
    function setFeeIfRevert(uint256 f) external { if (msg.sender != owner) revert NotOwner(); fee = f; } // gate
    function setPref(bool b) external { require(msg.sender != pool, "POOL"); pref[msg.sender] = b; }   // NOT a gate: excludes one address
}
