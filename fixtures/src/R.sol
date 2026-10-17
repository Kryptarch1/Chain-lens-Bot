// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;
// AccessControl-style fixture: DEFAULT_ADMIN held by deployer, MINTER_ROLE has NO holder, owner() renounced.
// mode 0: admin can grant MINTER_ROLE (-> mint enable-able later). mode 1: admin renounced too (nobody can grant).
contract R {
    string public name = "RoleFixture"; string public symbol = "RFX"; uint8 public decimals = 18;
    uint256 public totalSupply; mapping(address=>uint256) public balanceOf; mapping(address=>mapping(address=>uint256)) public allowance;
    bytes32 public constant DEFAULT_ADMIN_ROLE = 0x00; bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");
    mapping(bytes32=>mapping(address=>bool)) private _roles; mapping(bytes32=>bytes32) private _admins;
    address public owner;
    event Transfer(address indexed from, address indexed to, uint256 value); event Approval(address indexed owner, address indexed spender, uint256 value);
    event RoleGranted(bytes32 indexed role, address indexed account, address indexed sender);
    event RoleRevoked(bytes32 indexed role, address indexed account, address indexed sender);
    constructor(uint8 mode) {
        totalSupply = 1e27; balanceOf[msg.sender] = 1e27; emit Transfer(address(0), msg.sender, 1e27);
        _roles[DEFAULT_ADMIN_ROLE][msg.sender] = true; emit RoleGranted(DEFAULT_ADMIN_ROLE, msg.sender, msg.sender);
        owner = address(0);
        if (mode == 1) { _roles[DEFAULT_ADMIN_ROLE][msg.sender] = false; emit RoleRevoked(DEFAULT_ADMIN_ROLE, msg.sender, msg.sender); }
    }
    function hasRole(bytes32 r, address a) public view returns (bool) { return _roles[r][a]; }
    function getRoleAdmin(bytes32 r) public view returns (bytes32) { return _admins[r]; }
    function grantRole(bytes32 r, address a) external { require(hasRole(getRoleAdmin(r), msg.sender), "not admin"); _roles[r][a] = true; emit RoleGranted(r, a, msg.sender); }
    function revokeRole(bytes32 r, address a) external { require(hasRole(getRoleAdmin(r), msg.sender), "not admin"); _roles[r][a] = false; emit RoleRevoked(r, a, msg.sender); }
    function mint(address to, uint256 v) external { require(hasRole(MINTER_ROLE, msg.sender), "not minter"); totalSupply += v; balanceOf[to] += v; emit Transfer(address(0), to, v); }
    function approve(address s, uint256 v) external returns (bool) { allowance[msg.sender][s] = v; emit Approval(msg.sender, s, v); return true; }
    function transfer(address to, uint256 v) external returns (bool) { balanceOf[msg.sender] -= v; balanceOf[to] += v; emit Transfer(msg.sender, to, v); return true; }
    function transferFrom(address f, address to, uint256 v) external returns (bool) { allowance[f][msg.sender] -= v; balanceOf[f] -= v; balanceOf[to] += v; emit Transfer(f, to, v); return true; }
}
