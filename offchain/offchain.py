#!/usr/bin/env python

"""
CCCoin dApp.

This version uses a single off-chain witness for minting and rewards distribution.
The witness can also be audited by anyone with this code and access to the blockchain.

Version 2 will replace the single auditable witness with:
- Comittee of witnesses that are voted on by token holders. The witnesses then vote on the rewards distribution.
- 100% on-chain rewards computation in smart contracts.

---- INSTALL:

sudo add-apt-repository ppa:ethereum/ethereum
sudo add-apt-repository ppa:ethereum/ethereum-dev
sudo apt-get update

curl -sL https://deb.nodesource.com/setup_7.x | sudo -E bash -
sudo apt-get install -y nodejs

sudo npm install -g ethereumjs-testrpc

sudo npm install -g solc

sudo ln -s /usr/bin/nodejs /usr/bin/node

#pip install ethereum
#pip install ethereum-serpent

pip install py-solc
pip install ethjsonrpc

---- RUNNING:

testrpc -p 9999

python offchain.py deploy_dapp ## first time only
python offchain.py witness
python offchain.py web

"""

DATA_DIR = 'cccoin_conf/'
CONTRACT_ADDRESS_FN = DATA_DIR + 'cccoin_contract_address.txt'


from ethjsonrpc import EthJsonRpc
import json

# main_contract_code = \
# """
# pragma solidity ^0.4.6;

# contract CCCoin {
#     /* Used for vote logging of votes, tok lockup, etc. */
#     event LogMain(bytes);
#     function addLog(bytes val) { 
#         LogMain(val);
#     }
# }
# """

"""
Contract below implements StandardToken (https://github.com/ethereum/EIPs/issues/20) interface, in addition to:
 - Log event = vote, submit items, request tok -> lock.
 - Create user account.
 - Get user balances (tok / lock).
 - Withdraw tok.
 - Change user account owner address.
 - Change contract owner address.
 - Minting / rewards distribution (runnable only by MC, may be called multiple times per rewards cycle due to gas limits)
"""

main_contract_code = \
"""
pragma solidity ^0.4.0;

contract owned { 
    address owner;

    modifier onlyOwner {
        if (msg.sender != owner) throw;
        _;
    }
    function owned() { 
        owner = msg.sender; 
    }
}

contract mortal is owned {
    function kill() {
        if (msg.sender == owner) selfdestruct(owner);
    }
}

contract TokFactory is owned, mortal{ 

     event LogMain(bytes); 

     function addLog(bytes val) {
         LogMain(val);
     }

    mapping(address => address[]) public created;
    mapping(address => bool) public isToken; //verify without having to do a bytecode check.
    bytes public tokByteCode;
    address public verifiedToken;
    event tokenCreated(uint256 amount, address tokenAddress, address owner);
    Tok tok;

    function () { 
      throw; 
    }

    modifier noEther { 
      if (msg.value > 0) { throw; }
      _; 
    }

    modifier needTok { 
      if (address(tok) == 0x0) { throw; }
      _;
    }

    function TokFactory() {
      //upon creation of the factory, deploy a Token (parameters are meaningless) and store the bytecode provably.
      owner = msg.sender;
    }

    function getOwner() constant returns (address) { 
      return owner; 
    }

    function getTokenAddress() constant returns (address) { 
      // if (verifiedToken == 0x0) { throw; }
      return verifiedToken;
    } 

    //verifies if a contract that has been deployed is a validToken.
    //NOTE: This is a very expensive function, and should only be used in an eth_call. ~800k gas
    function verifyToken(address _tokenContract) returns (bool) {
      bytes memory fetchedTokenByteCode = codeAt(_tokenContract);

      if (fetchedTokenByteCode.length != tokByteCode.length) {
        return false; //clear mismatch
      }
      //starting iterating through it if lengths match
      for (uint i = 0; i < fetchedTokenByteCode.length; i ++) {
        if (fetchedTokenByteCode[i] != tokByteCode[i]) {
          return false;
        }
      }

      return true;
    }

    //for now, keeping this internal. Ideally there should also be a live version of this that any contract can use, lib-style.
    //retrieves the bytecode at a specific address.
    function codeAt(address _addr) internal returns (bytes o_code) {
      assembly {
          // retrieve the size of the code, this needs assembly
          let size := extcodesize(_addr)
          // allocate output byte array - this could also be done without assembly
          // by using o_code = new bytes(size)
          o_code := mload(0x40)
          // new "memory end" including padding
          mstore(0x40, add(o_code, and(add(add(size, 0x20), 0x1f), not(0x1f))))
          // store length in memory
          mstore(o_code, size)
          // actually retrieve the code, this needs assembly
          extcodecopy(_addr, add(o_code, 0x20), 0, size)
      }
    }

    function createTok(uint256 _initialAmount, string _name, uint8 _decimals, string _symbol) onlyOwner  returns (address) {
        tok = new Tok(_initialAmount, _name, _decimals, _symbol);
        created[msg.sender].push(address(tok));
        isToken[address(tok)] = true;
        // tok.transfer(owner, _initialAmount); //the creator will own the created tokens. You must transfer them.
        verifiedToken = address(tok); 
        tokByteCode = codeAt(verifiedToken);
        tokenCreated(_initialAmount, verifiedToken, msg.sender);
        return address(tok);
    }
    function rewardToken(address _buyer, uint256 _amount)  onlyOwner returns (bool) {
      return tok.transfer(_buyer, _amount); 
  }
}

contract StandardToken is owned, mortal{

    event Transfer(address sender, address to, uint256 amount);
    event Approval(address sender, address spender, uint256 value);

    /*
     *  Data structures
     */
    struct User {
      bool initialized; 
      address userAddress; 
      bytes32 userName;
      uint256 registerDate;
      uint8 blockVoteCount;    // number of votes this block
      uint256 currentBlock; 
      uint8 totalVotes; 
      mapping (uint8 => Post) votedContent;  // mapping of each vote to post 
    }

    struct Lock {
      uint256 amount; 
      uint256 unlockDate;
    }

    struct Post {
      bool initialized; 
      address creator; 
      bytes32 title;
      bytes32 content; 
      uint256 creationDate; 
      uint8 voteCount;     // total + or - 
      address[] voters;
      mapping (address => uint8) voteResult;     // -1 = downvote , 0 = no vote,  1 = upvote 
    }


    mapping (address => uint256) public balances;
    mapping (address => mapping (address => uint256)) allowed;
    uint256 public totalSupply;
    mapping (address => Lock[]) public lockedTokens;
    mapping (address => Post[]) public posts;
    uint8 public numUsers;
    mapping (address => User) public users;
    address[] userAddress; 

    
    /*
     *  Read and write storage functions
     */
    /// @dev Transfers sender's tokens to a given address. Returns success.
    /// @param _to Address of token receiver.
    /// @param _value Number of tokens to transfer.
    function transfer(address _to, uint256 _value) returns (bool success) {
        if (balances[msg.sender] >= _value && _value > 0) {
            balances[msg.sender] -= _value;
            balances[_to] += _value;
            Transfer(msg.sender, _to, _value);
            return true;
        }
        else {
            return false;
        }
    }

    /// @dev Allows allowed third party to transfer tokens from one address to another. Returns success.
    /// @param _from Address from where tokens are withdrawn.
    /// @param _to Address to where tokens are sent.
    /// @param _value Number of tokens to transfer.
    function transferFrom(address _from, address _to, uint256 _value) returns (bool success) {
        if (balances[_from] >= _value && allowed[_from][msg.sender] >= _value && _value > 0) {
            balances[_to] += _value;
            balances[_from] -= _value;
            allowed[_from][msg.sender] -= _value;
            Transfer(_from, _to, _value);
            return true;
        }
        else {
            return false;
        }
    }

    /// @dev Returns number of tokens owned by given address.
    /// @param _owner Address of token owner.
    function balanceOf(address _owner) constant returns (uint256 balance) {
        return balances[_owner];
    }

    /// @dev Sets approved amount of tokens for spender. Returns success.
    /// @param _spender Address of allowed account.
    /// @param _value Number of approved tokens.
    function approve(address _spender, uint256 _value) returns (bool success) {
        allowed[msg.sender][_spender] = _value;
        Approval(msg.sender, _spender, _value);
        return true;
    }

    /*
     * Read storage functions
     */
    /// @dev Returns number of allowed tokens for given address.
    /// @param _owner Address of token owner.
    /// @param _spender Address of token spender.
    function allowance(address _owner, address _spender) constant returns (uint256 remaining) {
      return allowed[_owner][_spender];
    }

}

contract Tok is StandardToken{ 

    address tokFactory; 

    string name; 
    uint8 decimals;
    string symbol; 

    modifier noEther { 
      if (msg.value > 0) { throw; }
      _; 
    }

    modifier controlled { 
        if (msg.sender != tokFactory) throw; 
        _;
    }
    
    function () {
        //if ether is sent to this address, send it back.
        throw;
    }
    
    function Tok(
        uint256 _initialAmount,
        string _tokenName,
        uint8 _decimalUnits,
        string _tokenSymbol
        ) noEther{
        tokFactory = msg.sender;
        balances[msg.sender] = _initialAmount;               // Give the TokFactory all initial tokens
        totalSupply = _initialAmount;                        // Update total supply
        name = _tokenName;                                   // Set the name for display purposes
        decimals = _decimalUnits;                            // Amount of decimals for display purposes
        symbol = _tokenSymbol;                               // Set the symbol for display purposes
    }

    function getUserAddress(address _user) noEther returns (address) { 
      return users[_user].userAddress; 
    }
    function getUserName(address _user) noEther returns (bytes32) { 
      return users[_user].userName; 
    }

    function register(bytes32 _username) noEther returns (bool success) { 
      User newUser = users[msg.sender];
      newUser.userName = _username;
      newUser.userAddress = msg.sender;
      newUser.registerDate = block.timestamp;
      return true; 

    }

    function mintToken(address _target, uint256 _mintedAmount) controlled {
        balances[_target] += _mintedAmount;
        totalSupply += _mintedAmount;
        Transfer(owner, _target, _mintedAmount);
    }

    function  post(bytes32 _title, bytes32 _content) noEther{
      Post[] posts = posts[msg.sender];
      posts.push(Post({creator: msg.sender, title: _title, content: _content, creationDate: block.timestamp, voteCount: 0}));
    }

    function vote(uint8 _postID, address _creator) noEther {
           User voter = users[msg.sender];
           Post postVotedOn = posts[_creator][_postID];
           if (voter.currentBlock == block.number) {
             // uint256 requiredLock =  (1 * numVotes) ** 3) + 100);  
             // uint256 lockBalance = lockBalance(msg.sender);
             // if (lockBalance < requiredLock) { throw;     }  
           }
           else { 
            voter.blockVoteCount = 0; 
            voter.currentBlock = block.number;
            uint256 totalLock = lockBalance(msg.sender);
            if (totalLock > 100) { 

            } 
           }
    }

    function lockBalance(address lockAccount)  constant returns (uint256) { 
      Lock[] lockedList = lockedTokens[lockAccount];
      uint256 total = 0;  
      for (uint8 i = 0; i < lockedList.length; i++) { 
        total += lockedList[i].amount; 
        }
      return total;
    }
    function calculateLockPayout(uint256 _amount) internal constant controlled { 
      for (uint8 i = 0; i < numUsers; i++) { 
         address temp = userAddress[i]; 
         User user = users[temp]; 
         uint256 userLockBalance = lockBalance(temp);

      }
    }
}
"""

class ContractWrapper:
    
    def __init__(self,
                 host = '127.0.0.1',
                 port = 9999,
                 #port = 8545,
                 events_callback = False,
                 confirm_states = {'PENDING':0,
                                   'CONFIRM_1':1,
                                   'CONFIRMED':15,
                                   'STALE':100,
                                   },
                 final_confirm_state = 'CONFIRMED',
                 contract_address = False,
                 ):
        """
        Simple contract wrapper, assists with deploying contract, sending transactions, and tracking event logs.
        
        Args:
          - `events_callback` will be called upon each state transition, according to `confirm_states`, until `final_confirm_state`.
          - `contract_address` contract address, from previous `deploy()` call.
        """
        
        self.confirm_states = confirm_states
        self.events_callback = events_callback
        
        if contract_address is False:
            if exists(CONTRACT_ADDRESS_FN):
                print ('Reading contract address from file...', CONTRACT_ADDRESS_FN)
                with open(CONTRACT_ADDRESS_FN) as f:
                    d = f.read()
                print ('GOT', d)
                self.contract_address = d
        else:
            self.contract_address = contract_address
        
        self.c = EthJsonRpc(host, port)
        
        self.pending_transactions = {}  ## {tx:callback}
        self.pending_logs = {}
        self.latest_block_number = -1
        
    def deploy(self):
        print ('DEPLOYING_CONTRACT...')        
        # get contract address
        xx = self.c.eth_compileSolidity(main_contract_code)
        #print ('GOT',xx)
        compiled = xx['code']
        contract_tx = self.c.create_contract(self.c.eth_coinbase(), compiled, gas=3000000)
        self.contract_address = self.c.get_contract_address(contract_tx)
        print ('DEPLOYED', self.contract_address)
        self.init_filter()

    def loop_once(self):
        if self.events_callback:
            self.check_transactions_committed()
            self.logs_poll_pending()
            self.check_logs_confirmed()
    
    def init_filter(self):
        print ('CREATING_FILTER')
        params = {"fromBlock": "0x01",
                  "address": self.contract_address,
                  }
        self.filter = str(self.c.eth_newFilter(params))
        print ('CREATED_FILTER', self.filter)
            

    def send_transaction(self, foo, value, callback = False, block = False):
        """
        1) Attempt to send transaction.
        2) Get first confirmation via transaction receipt.
        3) Re-check receipt again after N blocks pass.
        """
        tx = self.c.call_with_transaction(self.c.eth_coinbase(), self.contract_address, foo, value)
        
        if block:
            receipt = self.c.eth_getTransactionReceipt(tx) ## blocks to ensure transaction is mined
            if receipt['blockNumber']:
                self.latest_block_number = max(int(receipt['blockNumber'],16), self.latest_block_number)
        else:
            self.pending_transactions[tx] = callback
        
        return tx

    def check_transactions_committed(self):
        """
        Confirm transactions after `self.WAIT_BLOCKS` confirmations.
        """
        for tx, callback in self.pending_transactions.iteritems():
            receipt = self.c.eth_getTransactionReceipt(tx)
            
            if receipt['blockNumber']:
                block_number = int(receipt['blockNumber'],16)
            else:
                block_number = False

            self.latest_block_number = max(block_number, self.latest_block_number)
            
            if (block_number is not False) and (block_number + self.confirm_states['CONFIRMED'] >= self.latest_block_number):
                if callback is not False:
                    callback(receipt)
                del self.pending_transactions[tx]
    
    def read_transaction(self, foo, value):
        rr = self.c.call(self.c.eth_coinbase(), self.contract_address, foo, value)
        return rr
    
    def sign(self, user_address, value):
        rr = self.c.eth_sign(self.c.eth_coinbase(), self.contract_address, user_address, value)
        return rr
        
    def logs_poll_pending(self):
        """ 
        Pick up events from the event logs.
        """
        
        rr = self.c.eth_getFilterChanges(self.filter)
        
        for msg in rr:
            self.pending_logs[msg['transactionHash']] = msg
        
        return rr
    
    def check_logs_confirmed(self):
        """ 
        Check the confirmation state of previously picked up log events.

        Possible states:
        
        """
        for msg, tx, callback in self.pending_logs.items():
            
            receipt = self.c.eth_getTransactionReceipt(tx)
            
            if receipt['blockNumber']:
                block_number = int(receipt['blockNumber'],16)
            else:
                block_number = False
            
            self.latest_block_number = max(block_number, self.latest_block_number)
            
            if (block_number is not False) and (block_number + self.confirm_states['CONFIRMED'] >= self.latest_block_number):
                if self.events_callback is not False:
                    self.events_callback((msg, receipt, 'CONFIRMED'))
                del self.pending_logs[tx]


def deploy_dapp(via_cli = False):
    """
    Deploy new instance of this dApp to the blockchain.
    """
    
    fn = CONTRACT_ADDRESS_FN
    
    assert not exists(fn), ('Delete this file first:', fn)
    
    if not exists(DATA_DIR):
        mkdir(DATA_DIR)
    
    cont = ContractWrapper()
    
    addr = cont.deploy()
    
    with open(fn) as f:
        f.write(addr)
    
    print ('DONE', addr, '->', fn)


from os import urandom
    
def dumps_compact(h):
    return json.dumps(h, separators=(',', ':'), sort_keys=True)

def loads_compact(d):
    return json.loads(d, separators=(',', ':'))


class CCCoinAPI:
    def __init__(self,):

        self.cw = ContractWrapper(events_callback = self.event_logs_callback)

        self.latest_block_number = -1
        self.is_caught_up = False
        
        ## Key store for users on this node:
        
        self.private_key_store = {}
        
        ## Caches:
        
        self.items_by_hotness = []
        
        ## Items and Votes DB, split into pending and confirmed (>= confirm_wait_blocks).
        ## TODO Pending are ignored if not confirmed within a certain amount of time.
        
        self.items_pending = {}
        self.items_confirmed = {}
        self.votes_pending = {}
        self.votes_confirmed = {}
        
        ## Stores votes until rewards are paid:
        
        self.my_blind_votes =  {}          ## {vote_hash: vote_string}
        self.my_votes_unblinded = {}       ## {vote_hash: block_number}
    
    def event_logs_callback(self,
                            msg,
                            receipt,
                            confirm_level,
                            ):
        """
        """
        
        ## Proceed to next step for recently committed transactions:
        
        try:
            hh = loads_compact(msg['data']) ## de-hex needed?
        except:
            print ('BAD_MESSAGE', msg)
            return
        
        if confirm_level == 'CONFIRMED':
            if hh['t'] == 'vote_blinded':
                ## Got blinded vote, possibly from another node, or previous instance of self:

                if hh['sig'] in self.my_votes:
                    ## blind vote submitted from my node:
                    self.my_votes_confirmed[hh['sig']] = msg['blockNumber']
                    del self.my_votes_pending_confirm[hh['sig']]
                else:
                    ## blind vote submitted from another node:
                    pass

            elif hh['t'] == 'vote_unblinded':
                ## Got unblinded vote, possibly from another node, or previous instance of self:

                if hh['sig'] in self.my_votes_confirmed:                    
                    pass

            elif hh['t'] == 'vote_unblinded':
                pass


                ## Unblind votes that need to be unblinded:
            
            for sig, block_number in self.my_votes_confirmed.iteritems():
                pass
            
            
    
    def deploy_contract(self,):
        """
        Create new instance of dApp on blockchain.
        """
        self.cw.deploy()
    
    
    def create_account(self,):
        """
        Register a new account, requires a fee.
        """
        pass
    
    
    def post_item(self,
                  user_id,
                  title,
                  url,
                  nonce = False,
                  ):
        
        rr = dumps_compact({'t':'post', 'title': title, 'url':'url'})
        
        tx = self.cw.send_transaction('addLog(bytes)',
                                      [rr],
                                      )
    
    def submit_blind_vote(self,
                          user_id,
                          votes,
                          nonce,
                          user_private_key = False,
                          ):
        """
        Submit blinded vote(s) to blockchain.

        Process:
        1) Sign and submit vote.
        2) Wait for vote to appears in blockchain.
        3) Wait N blocks after vote appears in blockchain.
        4) Unblind vote.
        
        Args:
        
        `votes` is a list of dicts. "direction" is 1 or 0:
            [{'i':item_id,'d':direction,},...]
        
        `nonce` prevents out-of-order submission of votes & double sending of votes.
        
        Must either:
          - User was created via this node, so private key can be looked up, or,
          - Private key is passed.
        """
        
        h = {'u':user_id,
             'v':votes,
             }
        
        vv = dumps_compact(h)
        
        print ('SIGNING_VOTE', vv)
        
        vs = self.cw.sign(user_address, vv)
        
        print ('SIGNED_VOTE', vs)
        
        #vs = self.cw.web3_sha3(vs)
        
        self.my_blind_votes[vs] = vv

        rr = dumps_compact({'t':'vote_blinded', 'sig': vs})
        
        tx = self.cw.send_transaction('addLog(bytes)',
                                      [rr],
                                      callback = lambda: self.unblind_votes(vv, vs),  ## Wait for full confirmation.
                                      )
        
    def unblind_votes(self,
                      vote_string,
                      vote_sig,
                      ):
        """
        """
        
        rr = dumps_compact({'t':'vote_unblinded', 'sig': vote_sig, 'orig': vote_string})
                
        tx = self.cw.send_transaction('addLog(bytes)',
                                      [rr],
                                      )

    def lockup_tok(self):
        tx = self.cw.send_transaction('lockupTok(bytes)', [rr])

    def get_balances(self,
                     user_id,
                     ):
        xx = self.cw.read_transaction('lockupTok(bytes)', [rr])
        rr = loads_compact(xx['data'])
        return rr

    def withdraw_lock(self,):
        tx = self.cw.send_transaction('withdrawTok(bytes)', [rr])
    
    def distribute_rewards(self,):
        """
        """
        tx = self.cw.send_transaction('distributeRewards(bytes)', [rr])
    
    def get_hot_items(self,):
        pass


def witness():
    """
    Run witness for vote computation and rewards distribution.
    
    TODO - optionally hook this into Tornado event loop.
    """
    xx = CCCoinAPI()

    while True:
        xx.loop_once()
        sleep(0.5)
    
##
####
##


def trend_detection(input_gen,
                    window_size = 7,
                    prev_window_multiple = 1,
                    empty_val_2 = 1,
                    input_is_absolutes = False, ## otherwise, must convert to differences
                    do_ttl = False,
                    ttl_halflife_steps = 1,
                    ):
    """
    Basic in-memory KL-divergence based trend detection, with some helpers.
    """
    
    from math import log
    from sys import maxint
    
    tot_window_size = window_size + window_size * prev_window_multiple
    
    all_ids = set()
    windows = {}        ## {'product_id':[1,2,3,4]}

    the_prev = {}       ## {item_id:123}
    the_prev_step = {}  ## {item_id:step}
    
    max_score = {}      ## {item_id:score}
    max_score_time = {} ## {item_id:step_num}
    
    first_seen = {}     ## {item_id:step_num}
    
    output = []

    for c,hh in enumerate(input_gen):

        output_lst = []
        
        #step_num = hh['step']
        
        ## For seen items:
        for item_id,value in hh['values'].iteritems():
            
            if item_id not in first_seen:
                first_seen[item_id] = c
                        
            all_ids.add(item_id)

            if item_id not in windows:
                windows[item_id] = [0] * tot_window_size

            if item_id not in the_prev:
                the_prev[item_id] = value
                the_prev_step[item_id] = c - 1
                
            if input_is_absolutes:
                
                nn = (value - the_prev[item_id]) / float(c - the_prev_step[item_id])
                                
                if item_id == 'a':
                    print 'c:',c,'value:',value,'the_prev:',the_prev[item_id],'ss:',(c - the_prev_step[item_id] + 1)
                    print (item_id,value,nn)
                    raw_input_enter()
                
                windows[item_id].append(nn)
                                
            else:
                windows[item_id].append(value)
            
            windows[item_id] = windows[item_id][-tot_window_size:]
            
            the_prev[item_id] = value
            the_prev_step[item_id] = c

        # Fill in for unseen items:
        for item_id in all_ids.difference(hh['values'].keys()):
            windows[item_id].append(0)
            
            windows[item_id] = windows[item_id][-tot_window_size:]

        if c < tot_window_size:
            continue

        
        ## Calculate on windows:
        for item_id,window in windows.iteritems():

            window = [max(empty_val_2,x) for x in window]
            
            cur_win = window[-window_size:]
            prev_win = window[:-window_size]
            
            cur = sum(cur_win) / float(window_size)
            prev = sum(prev_win) / float(window_size * prev_window_multiple)  #todo - seen for first time?
            
            if len([1 for x in prev_win if x > empty_val_2]) < window_size:
                #ignore if too many missing
                score = 0
            else:
                score = prev * log( cur / prev )
            
            prev_score = max_score.get(item_id, -maxint)
            
            if score > prev_score:
                max_score_time[item_id] = c
                
            max_score[item_id] = max(prev_score, score)

            #Sd(h, t) = SM(h) * (0.5)^((t - tmax)/half-life)
            if do_ttl:
                score = max_score[item_id] * (0.5 ** ((c - max_score_time[item_id])/float(ttl_halflife_steps)))

            output_lst.append((score,item_id,window))
            
        output_lst.sort(reverse=True)
        output.append(output_lst)

    return output

def test_trend_detection():
    trend_detection(input_gen = [{'values':{'a':5,'b':2,}},
                                 {'values':{'a':7,'b':2,}},
                                 {'values':{'a':9,'b':2,}},
                                 {'values':{'a':11,'b':4,}},
                                 {'values':{'a':13,'b':5,}},
                                 {'values':{'a':16,'b':6,'c':1,}},
                                 {'values':{'a':17,'b':7,'c':1,'d':1}},
                                 ],
                    window_size = 2,
                    prev_window_multiple = 1,
                    input_is_absolutes = True,
                    do_ttl = True,
                    )

##
#### Generic helper functions for web server:
##

def intget(x,
           default = False,
           ):
    try:
        return int(x)
    except:
        return default

def floatget(x,
             default = False,
             ):
    try:
        return float(x)
    except:
        return default

    
def raw_input_enter():
    print 'PRESS ENTER...'
    raw_input()


def ellipsis_cut(s,
                 n=60,
                 ):
    s=unicode(s)
    if len(s)>n+1:
        return s[:n].rstrip()+u"..."
    else:
        return s


def shell_source(fn_glob,
                 allow_unset = False,
                 ):
    """
    Source bash variables from file. Input filename can use globbing patterns.
    
    Returns changed vars.
    """
    import os
    from os.path import expanduser
    from glob import glob
    from subprocess import check_output
    from pipes import quote
    
    orig = set(os.environ.items())
    
    for fn in glob(fn_glob):
        
        fn = expanduser(fn)
        
        print ('SOURCING',fn)
        
        rr = check_output("source %s; env -0" % quote(fn),
                          shell = True,
                          executable = "/bin/bash",
                          )
        
        env = dict(line.split('=',1) for line in rr.split('\0'))
        
        changed = [x for x in env.items() if x not in orig]
        
        print ('CHANGED',fn,changed)

        if allow_unset:
            os.environ.clear()
        
        os.environ.update(env)
        print env
    
    all_changed = [x for x in os.environ.items() if x not in orig]
    return all_changed
    

def terminal_size():
    """
    Get terminal size.
    """
    h, w, hp, wp = struct.unpack('HHHH',fcntl.ioctl(0,
                                                    termios.TIOCGWINSZ,
                                                    struct.pack('HHHH', 0, 0, 0, 0),
                                                    ))
    return w, h

def usage(functions,
          glb,
          entry_point_name = False,
          ):
    """
    Print usage of all passed functions.
    """
    try:
        tw,th = terminal_size()
    except:
        tw,th = 80,40
                   
    print
    
    print 'USAGE:',(entry_point_name or ('python ' + sys.argv[0])) ,'<function_name>'
        
    print
    print 'Available Functions:'
    
    for f in functions:
        ff = glb[f]
        
        dd = (ff.__doc__ or '').strip() or 'NO_DOCSTRING'
        if '\n' in dd:
            dd = dd[:dd.index('\n')].strip()

        ee = space_pad(f,ch='.',n=40)
        print ee,
        print ellipsis_cut(dd, max(0,tw - len(ee) - 5))
    
    sys.exit(1)

    
def set_console_title(title):
    """
    Set console title.
    """
    try:
        title = title.replace("'",' ').replace('"',' ').replace('\\',' ')
        cmd = "printf '\033k%s\033\\'" % title
        system(cmd)
    except:
        pass


def setup_main(functions,
               glb,
               entry_point_name = False,
               ):
    """
    Helper for invoking functions from command-line.
    """
        
    if len(sys.argv) < 2:
        usage(functions,
              glb,
              entry_point_name = entry_point_name,
              )
        return

    f=sys.argv[1]
    
    if f not in functions:
        print 'FUNCTION NOT FOUND:',f
        usage(functions,
              glb,
              entry_point_name = entry_point_name,
              )
        return

    title = (entry_point_name or sys.argv[0]) + ' '+ f
    
    set_console_title(title)
    
    print 'STARTING ',f + '()'

    ff=glb[f]

    ff(via_cli = True) ## New: make it easier for the functions to have dual CLI / API use.


##
### Web frontend:
##


import json
import ujson
import tornado.ioloop
import tornado.web
from time import time
from tornadoes import ESConnection

import tornado
import tornado.options
import tornado.web
import tornado.template
import tornado.gen
import tornado.auth
from tornado.web import RequestHandler
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.options import define, options

from os import mkdir, listdir, makedirs, walk, rename, unlink
from os.path import exists,join,split,realpath,splitext,dirname

class Application(tornado.web.Application):
    def __init__(self,
                 ):
        
        handlers = [(r'/',handle_front,),
                    #(r'.*', handle_notfound,),
                    ]
        
        settings = {'template_path':join(dirname(__file__), 'templates_cccoin'),
                    'static_path':join(dirname(__file__), 'static_cccoin'),
                    'xsrf_cookies':False,
                    }
        
        tornado.web.Application.__init__(self, handlers, **settings)


class BaseHandler(tornado.web.RequestHandler):
    
    def __init__(self, application, request, **kwargs):
        RequestHandler.__init__(self, application, request, **kwargs)
        
        self._current_user=False
        
        self.loader=tornado.template.Loader('templates_cccoin/')
    
    @property
    def io_loop(self,
                ):
        if not hasattr(self.application,'io_loop'):
            self.application.io_loop = IOLoop.instance()
        return self.application.io_loop
        
    def get_current_user(self,):
        return self._current_user

    @property
    def cccoin(self,
               ):
        if not hasattr(self.application,'cccoin'):
            self.application.cccoin = CCCoinAPI()
        return self.application.cccoin
        
    
    @tornado.gen.engine
    def render_template(self,template_name, kwargs):
        """
        Central point to customize what variables get passed to templates.        
        """
        
        t0 = time()
        
        if 'self' in kwargs:
            kwargs['handler'] = kwargs['self']
            del kwargs['self']
        else:
            kwargs['handler'] = self
        
        r = self.loader.load(template_name).generate(**kwargs)
        
        print ('TEMPLATE TIME',(time()-t0)*1000)
        
        self.write(r)
        self.finish()
    
    def render_template_s(self,template_s,kwargs):
        """
        Render template from string.
        """
        
        t=Template(template_s)
        r=t.generate(**kwargs)
        self.write(r)
        self.finish()
        
    def write_json(self,
                   hh,
                   sort_keys = False,
                   indent = 4, #Set to None to do without newlines.
                   ):
        """
        Central point where we can customize the JSON output.
        """
        if 'error' in hh:
            print ('ERROR',hh)
        
        self.set_header("Content-Type", "application/json")
        
        self.write(json.dumps(hh,
                              sort_keys = sort_keys,
                              indent = 4,
                              ) + '\n')
        self.finish()
        

    def write_error(self,
                    status_code,
                    **kw):
        
        self.write('INTERNAL_ERROR')


class handle_front(BaseHandler):
    
    @tornado.gen.coroutine
    def get(self):
        
        items = self.cccoin.get_hot_items()
        
        tmpl = \
        """
        <html>
          <head></head>
          <body>
           <h1>CCCoin</h1>
           {% for item in items %}
              <a href="{{ item['link'] }}>
                 {{ item['score'] }}
                 point{{ item['score'] != 1 and 's' or '' }}:
                 {{ item['title'] }}
              </a>
              <br>
           {% end %}
          </body>
        </html>
        """
        
        self.render_template_s(tmpl, locals())

class handle_submit_item(BaseHandler):
    
    @tornado.gen.coroutine
    def post(self):
        
        user_id = self.get_current_user()
        
        title = intget(self.get_argument('title',''), False)
        url = intget(self.get_argument('url',''), False)
        nonce = intget(self.get_argument('nonce',''), False)
        
        assert title
        assert url
        
        self.cccoin.post_item(self,
                              user_id,
                              title,
                              url,
                              nonce,
                              )
        
        self.redirect('/')

        
class handle_vote(BaseHandler):
    
    @tornado.gen.coroutine
    def post(self):

        user_id = self.get_current_user()
        
        item_id = intget(self.get_argument('item_id',''), False)
        direction = intget(self.get_argument('direction',''), False)
        
        assert item_id
        assert direction in [0, 1]
        
        votes = {'i':item_id,
                 'd':direction,
                 }
        
        nonce = intget(self.get_argument('nonce',''), False)
        
        self.cccoin.submit_blind_vote(user_id,
                                      votes,
                                      nonce,
                                      )

        self.redirect('/')


def web(port = 34567,
        via_cli = False,
        ):
    """
    Start CCCoin web server.
    """
        
    print ('BINDING',port)
    
    try:
        tornado.options.parse_command_line()
        http_server = HTTPServer(Application(),
                                 xheaders=True,
                                 )
        http_server.bind(port)
        http_server.start(16) # Forks multiple sub-processes
        tornado.ioloop.IOLoop.instance().set_blocking_log_threshold(0.5)
        IOLoop.instance().start()
        
    except KeyboardInterrupt:
        print 'Exit'
    
    print ('WEB_STARTED')


functions=['deploy_dapp',
           'witness',
           'web',
           ]

def main():    
    setup_main(functions,
               globals(),
               'offchain.py',
               )

if __name__ == '__main__':
    main()

