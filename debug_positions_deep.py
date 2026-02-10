from upstox_wrapper import UpstoxWrapper
from colorama import init, Fore, Style
import pprint

init(autoreset=True)

def main():
    print(f"{Fore.CYAN}Fetching positions from Upstox...{Style.RESET_ALL}")
    api = UpstoxWrapper()
    positions = api.get_positions()
    
    if not positions:
        print(f"{Fore.RED}No positions found!{Style.RESET_ALL}")
        return

    print(f"{Fore.GREEN}Found {len(positions)} positions:{Style.RESET_ALL}")
    
    for p in positions:
        print("\n" + "="*50)
        symbol = getattr(p, 'trading_symbol', getattr(p, 'tradingsymbol', 'Unknown'))
        print(f"SYMBOL: {Fore.YELLOW}{symbol}{Style.RESET_ALL}")
        
        # 1. Print all attributes that might look like a price
        print(f"\n{Fore.CYAN}--- PRICE ATTRIBUTES ---{Style.RESET_ALL}")
        count = 0
        for attr in dir(p):
            if not attr.startswith('__') and ('price' in attr.lower() or 'avg' in attr.lower() or 'val' in attr.lower()):
                try:
                    val = getattr(p, attr)
                    if not callable(val):
                        print(f"  {attr:<25}: {val}")
                        count += 1
                except Exception as e:
                    pass
        
        if count == 0:
            print("  (None found)")

        # 2. Try to dump standard Upstox fields if they exist
        print(f"\n{Fore.CYAN}--- KEY FIELDS ---{Style.RESET_ALL}")
        for field in ['quantity', 'net_quantity', 'realised', 'unrealised', 'buy_quantity', 'sell_quantity']:
            if hasattr(p, field):
                 print(f"  {field:<25}: {getattr(p, field)}")

        # 3. Dump full dict if possible
        # print(f"\n{Fore.CYAN}--- RAW OBJECT REPR ---{Style.RESET_ALL}")
        # print(p)

if __name__ == "__main__":
    main()
