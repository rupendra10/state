import os
import time
import random
import upstox_client
from upstox_client.rest import ApiException
import config
import threading

class UpstoxWrapper:
    def __init__(self, access_token=None):
        """
        Initialize Upstox Client.
        For MVP/Auto-trading, we assume we have a valid access_token.
        In a full app, we'd handle the OAuth Code -> Token flow.
        """
        # Priority: Constructor Arg > Config File > Env Var
        self.access_token = access_token or config.UPSTOX_ACCESS_TOKEN or os.getenv('UPSTOX_ACCESS_TOKEN')
        if not self.access_token:
            print("WARNING: No Upstox Access Token provided. Set UPSTOX_ACCESS_TOKEN env var.")
            self.access_token = "" # Avoid NoneType error in client lib
        
        self.configuration = upstox_client.Configuration()
        self.configuration.access_token = self.access_token
        
        # API Instances
        self.api_client = upstox_client.ApiClient(self.configuration)
        self.history_api = upstox_client.HistoryApi(self.api_client)
        self.order_api = upstox_client.OrderApi(self.api_client)
        self.user_api = upstox_client.UserApi(self.api_client)
        self.market_quote_api = upstox_client.MarketQuoteApi(self.api_client)
        self.portfolio_api = upstox_client.PortfolioApi(self.api_client)
        self.market_quote_v3_api = upstox_client.MarketQuoteV3Api(self.api_client)
        
        # Rate limiting state
        self._last_call_time = 0
        self._rate_limit_lock = threading.Lock()
        self._mandatory_delay = 1.0 # 1 second between any two API calls

    def _wait_for_rate_limit(self):
        """Ensures at least _mandatory_delay seconds have passed since the last API call."""
        with self._rate_limit_lock:
            now = time.time()
            elapsed = now - self._last_call_time
            if elapsed < self._mandatory_delay:
                wait_to_sleep = self._mandatory_delay - elapsed
                time.sleep(wait_to_sleep)
            self._last_call_time = time.time()

    def _chunk_list(self, input_list, chunk_size):
        """Yield successive chunk_size-sized chunks from input_list."""
        for i in range(0, len(input_list), chunk_size):
            yield input_list[i : i + chunk_size]

    def _safe_ltp_call(self, symbol, max_retries=5):
        """
        Helper to call the ltp API with aggressive retry logic for 429 (Too Many Requests).
        Uses exponential backoff with jitter and mandatory inter-call delay.
        """
        retries = 0
        while retries <= max_retries:
            self._wait_for_rate_limit()
            try:
                return self.market_quote_api.ltp(symbol=symbol, api_version='2.0')
            except ApiException as e:
                if e.status == 429:
                    retries += 1
                    if retries > max_retries:
                        print(f"ERROR: Max retries ({max_retries}) reached for 429 error on {symbol}.")
                        raise e
                    
                    # More aggressive backoff: 5, 10, 20, 40, 80...
                    wait_time = (5 * (2 ** (retries - 1))) + (random.randint(0, 2000) / 1000)
                    print(f"CRITICAL WARNING: 429 Too Many Requests. Burst detected. Retrying in {wait_time:.2f}s (Attempt {retries}/{max_retries})...")
                    time.sleep(wait_time)
                elif e.status == 401:
                    print("CRITICAL: Unauthorized. Check your UPSTOX_ACCESS_TOKEN.")
                    os._exit(1)
                else:
                    raise e
        return None

    def get_spot_price(self, instrument_key):
        """
        Get latest Last Traded Price (LTP) for an instrument.
        Example instrument_key: 'NSE_INDEX|Nifty 50'
        """
        try:
            # Full market quote
            api_response = self._safe_ltp_call(symbol=instrument_key)
            if api_response and api_response.status == 'success':
                # The API sometimes returns keys with : instead of | in the dictionary
                res_key = instrument_key.replace('|', ':')
                if instrument_key in api_response.data:
                    return api_response.data[instrument_key].last_price
                elif res_key in api_response.data:
                    return api_response.data[res_key].last_price
                
                # Fallback: if data has items, return first one's price
                if api_response.data:
                    first_key = list(api_response.data.keys())[0]
                    return api_response.data[first_key].last_price
                    
            return None
        except Exception as e:
            print(f"Exception when fetching spot price: {e}")
            return None

    def get_option_chain_quotes(self, instrument_keys):
        """
        Get quotes for a list of option keys to build a chain.
        """
        if not instrument_keys:
            return {}
            
        try:
            full_data = {}
            # Upstox LTP API limit is 100 per call
            for chunk in self._chunk_list(instrument_keys, 100):
                symbols_str = ",".join(chunk)
                api_response = self._safe_ltp_call(symbol=symbols_str)
                if api_response and api_response.status == 'success':
                    full_data.update(api_response.data)
            
            if full_data:
                # Normalize keys in response back to | (pipe) to match our internal keys
                normalized_data = {}
                for key, val in full_data.items():
                    token = getattr(val, 'instrument_token', None)
                    if token:
                        norm_token = token.replace(':', '|')
                        normalized_data[norm_token] = val
                    
                    norm_key = key.replace(':', '|')
                    normalized_data[norm_key] = val
                    
                return normalized_data
            return {}
        except Exception as e:
            print(f"Error getting quotes: {e}")
            return {}

    def cancel_order(self, order_id):
        """
        Cancel a pending order.
        """
        try:
            self._wait_for_rate_limit()
            api_response = self.order_api.cancel_order(order_id, api_version='2.0')
            if api_response.status == 'success':
                print(f"Order {order_id} cancelled successfully.")
                return True
            else:
                msg = getattr(api_response, 'message', 'Unknown Error')
                if 'UDAPI100040' in str(msg) or 'already cancelled/rejected/completed' in str(msg):
                    # This is actually a 'soft' success for our polling logic
                    return "ALREADY_CLOSED"
                print(f"Failed to cancel order {order_id}: {msg}")
                return False
        except ApiException as e:
            # Handle 400 Bad Request which Upstox sends for "Already Cancelled/Completed"
            import json
            try:
                err_data = json.loads(e.body)
                err_code = err_data.get('errors', [{}])[0].get('errorCode', '')
                err_msg = err_data.get('errors', [{}])[0].get('message', '')
                
                if 'UDAPI100040' in str(err_code) or 'already cancelled/rejected/completed' in str(err_msg):
                    print(f"Order {order_id} cancellation failed as it is already closed (UDAPI100040). Returning ALREADY_CLOSED.")
                    return "ALREADY_CLOSED"
                
                print(f"API Error cancelling order {order_id}: {e}")
            except Exception as parse_err:
                print(f"API Error cancelling order {order_id} (Parse Failed): {e}")
            
            return False
        except Exception as e:
            print(f"Error cancelling order {order_id}: {e}")
            return False

    def search_instruments(self, query):
        """
        Search for instruments to find keys.
        NOTE: This is just a placeholder. In production, downloading the full instrument master CSV is preferred for speed.
        """
        pass
    
    def place_order(self, instrument_key, quantity, transaction_type, order_type='MARKET', product='D', tag=None):
        """
        Place a buy/sell order.
        transaction_type: 'BUY' or 'SELL'
        product: 'D' (Delivery) or 'I' (Intraday)
        """
        order_tag = tag if tag else config.ORDER_TAG_PREFIX
        body = upstox_client.PlaceOrderRequest(
            quantity=quantity,
            product=config.ORDER_PRODUCT,
            validity=config.ORDER_VALIDITY,
            price=0.0,
            tag=order_tag,
            instrument_token=instrument_key,
            order_type=order_type,
            transaction_type=transaction_type,
            disclosed_quantity=0,
            trigger_price=0.0,
            is_amo=False
        )
        try:
            self._wait_for_rate_limit()
            api_response = self.order_api.place_order(body, api_version='2.0')
            if api_response.status == 'success':
                order_id = api_response.data.order_id
                print(f"Order Placed Successfully. ID: {order_id}. Waiting for fill...")
                
                # Polling for status (increased to 60s: 120 * 0.5s)
                max_retries = 120
                for attempt in range(max_retries):
                    time.sleep(0.5)
                    status_resp = self.get_order_details(order_id)
                    
                    filled_qty = status_resp.get('filled_quantity', 0)
                    total_qty = status_resp.get('quantity', 0)
                    is_filled_by_qty = (total_qty > 0 and filled_qty >= total_qty)

                    if status_resp['status'] == 'complete' or is_filled_by_qty:
                        if not status_resp['status'] == 'complete':
                             print(f"Order {order_id} detected as FILLED via Quantity Check ({filled_qty}/{total_qty}) despite status '{status_resp['status']}'.")
                        return {'status': 'success', 'avg_price': status_resp['avg_price'], 'order_id': order_id}
                    elif status_resp['status'] == 'rejected':
                        reason = status_resp.get('message', 'Rejected by broker')
                        print(f"CRITICAL: Order {order_id} REJECTED: {reason}")
                        return {'status': 'error', 'message': f"Order Rejected: {reason}"}
                    elif status_resp['status'] == 'cancelled':
                        print(f"CRITICAL: Order {order_id} CANCELLED.")
                        return {'status': 'error', 'message': "Order Cancelled"}
                
                # TIMEOUT: Mandatory Cancellation to prevent ghost positions
                print(f"WARNING: Order {order_id} TIMEOUT. Attempting immediate cancellation.")
                cancel_res = self.cancel_order(order_id)
                
                # Wait 1s for broker state to stabilize/propagate
                time.sleep(1.0)
                
                # Final check after cancellation attempt
                final_status = self.get_order_details(order_id)
                if final_status['status'] == 'complete':
                     print(f"Order {order_id} FILLED during cancellation window. Treating as success.")
                     return {'status': 'success', 'avg_price': final_status['avg_price'], 'order_id': order_id}
                
                # If cancel_order failed (including ALREADY_CLOSED), check status one last time
                if cancel_res is not True:
                    # The order is closed or cancel failed/rejected.
                    # We must verify if it was FILLED to avoid "Weekly Exit Order FAILED".
                    # The Status API might be lagging. We will retry fetching status for a few seconds.
                    reason_tag = "ALREADY_CLOSED" if cancel_res == "ALREADY_CLOSED" else "CANCEL_FAILED"
                    print(f"Order {order_id} cancellation didn't return success ({reason_tag}). Verifying final status...")
                    
                    for verify_attempt in range(5):
                        time.sleep(1.0) # Wait for status to settle
                        final_status = self.get_order_details(order_id)
                        fs = final_status['status']
                        filled_qty = final_status.get('filled_quantity', 0)
                        total_qty = final_status.get('quantity', 0)
                        is_filled_by_qty = (total_qty > 0 and filled_qty >= total_qty)
                        
                        if fs == 'complete' or is_filled_by_qty:
                             print(f"Order {order_id} verified as COMPLETE (Qty: {filled_qty}/{total_qty}) during verification.")
                             return {'status': 'success', 'avg_price': final_status['avg_price'], 'order_id': order_id}
                        elif fs in ['cancelled', 'rejected']:
                             print(f"Order {order_id} verified as {fs.upper()} during verification.")
                             return {'status': 'error', 'message': f"Order {order_id} was {fs}."}
                        
                        print(f"Order {order_id} status is '{fs}', waiting for update... ({verify_attempt+1}/5)")
                    
                    # If we are here, status is still ambiguous (e.g. 'open', 'put order req received') but Cancel failed.
                    # This implies a massive broker inconsistency or stuck state.
                    # Safety: We assume it's DONE/FILLED to avoid Double Entry. 
                    
                    print(f"WARNING: Order {order_id} status stuck at '{final_status.get('status')}' but Cancel failed. Assuming FILLED to prevent Double Entry.")
                    # Best guess estimate for price if not available
                    return {'status': 'success', 'avg_price': final_status.get('avg_price', 0.0), 'order_id': order_id, 'warning': 'Ambiguous Status'}
                    
                    # return {'status': 'error', 'message': f"Order {order_id} was already {final_status['status']} when cancellation was attempted."}

                return {'status': 'error', 'message': f"Order Timeout: Not filled within 60 seconds. Cancellation of {order_id} requested."}
            else:
                return {'status': 'error', 'message': getattr(api_response, 'message', 'Unknown API Error')}
        except ApiException as e:
            # Handle specific Upstox error messages
            import json
            error_msg = str(e)
            try:
                err_data = json.loads(e.body)
                error_msg = err_data.get('errors', [{}])[0].get('message', str(e))
            except:
                pass
            print(f"CRITICAL ERROR: Order placement failed - {error_msg}")
            return {'status': 'error', 'message': error_msg}
        except Exception as e:
            print(f"CRITICAL UNKNOWN ERROR: {e}")
            return {'status': 'error', 'message': str(e)}

    def get_order_details(self, order_id):
        """
        Fetch details of a specific order to check its status and average price.
        """
        try:
            self._wait_for_rate_limit()
            api_response = self.order_api.get_order_details(order_id=order_id, api_version='2.0')
            if api_response.status == 'success':
                # Upstox order_details can be a list or single object
                data = api_response.data
                if isinstance(data, list) and len(data) > 0:
                    order = data[0]
                else:
                    order = data
                
                status = getattr(order, 'status', '').lower()
                
                avg_prc_raw = getattr(order, 'average_price', 0.0)
                try:
                    avg_price = float(avg_prc_raw) if avg_prc_raw is not None else 0.0
                except (ValueError, TypeError):
                    avg_price = 0.0
                    
                message = getattr(order, 'status_message', 'No message')
                filled_qty = int(getattr(order, 'filled_quantity', 0))
                qty = int(getattr(order, 'quantity', 0))
                
                return {
                    'status': status,
                    'avg_price': avg_price,
                    'message': message,
                    'filled_quantity': filled_qty,
                    'quantity': qty
                }
        except Exception as e:
            print(f"Error fetching order details for {order_id}: {e}")
        
        return {'status': 'error', 'message': 'Failed to fetch status'}

    def get_funds(self):
        """
        Get available margin/funds for the user.
        """
        try:
            self._wait_for_rate_limit()
            api_response = self.user_api.get_user_fund_margin(api_version='2.0')
            if api_response.status == 'success':
                # Upstox SDK returns objects. 
                # Structure is usually data.equity.available_margin
                data = getattr(api_response, 'data', None)
                if data:
                    equity = getattr(data, 'equity', None)
                    if equity:
                        return getattr(equity, 'available_margin', 0.0)
        except Exception as e:
            print(f"Error fetching funds: {e}")
        return 0.0

    def get_positions(self):
        """
        Fetch active positions from the portfolio.
        """
        try:
            self._wait_for_rate_limit()
            api_response = self.portfolio_api.get_positions(api_version='2.0')
            if api_response.status == 'success':
                return api_response.data
            return []
        except Exception as e:
            print(f"Exception when fetching positions: {e}")
            return None

    def _safe_greek_call(self, symbols_str, max_retries=3):
        """
        Helper to call Greeks API with retries for 5xx/429 errors.
        """
        retries = 0
        while retries <= max_retries:
            self._wait_for_rate_limit()
            try:
                return self.market_quote_v3_api.get_market_quote_option_greek(instrument_key=symbols_str)
            except ApiException as e:
                # Retry on Server Errors (5xx) or Rate Limit (429)
                if e.status >= 500 or e.status == 429:
                    retries += 1
                    if retries > max_retries:
                        print(f"ERROR: Max retries ({max_retries}) reached for Greeks API (Status {e.status}).")
                        raise e
                    
                    wait_time = (2 * (2 ** (retries - 1))) + (random.randint(0, 1000) / 1000)
                    print(f"WARNING: Greeks API Error {e.status}. Retrying in {wait_time:.2f}s...")
                    time.sleep(wait_time)
                else:
                    raise e
        return None

    def get_option_greeks(self, instrument_keys):
        """
        Fetch real-time Greeks for a list of option keys using v3 API.
        """
        if not instrument_keys:
            return {}
            
        normalized_greeks = {}
        # Upstox Option Greek API limit is 50 per call (UDAPI100076)
        
        # We iterate chunks and handle failures per chunk to avoid total data loss
        for chunk in self._chunk_list(instrument_keys, 50):
            try:
                symbols_str = ",".join(chunk)
                api_response = self._safe_greek_call(symbols_str)
                
                if api_response and api_response.status == 'success' and api_response.data:
                    # Normalize keys and extract delta
                    # DEBUG LOGGING (Disabled)
                    # print(f"[DEBUG] Greeks Response Keys: {list(api_response.data.keys())}")
                    for key, val in api_response.data.items():
                        
                        norm_key = key.replace(':', '|')
                        normalized_greeks[norm_key] = {
                            'delta': float(val.delta) if val.delta is not None else None,
                            'theta': float(val.theta) if val.theta is not None else None,
                            'gamma': float(val.gamma) if val.gamma is not None else None,
                            'vega': float(val.vega) if val.vega is not None else None,
                            'iv': float(val.iv) if val.iv is not None else None
                        }
            except Exception as e:
                print(f"Error fetching Greeks chunk: {e}")
                # Continue to next chunk instead of failing execution
                continue
                
        return normalized_greeks
