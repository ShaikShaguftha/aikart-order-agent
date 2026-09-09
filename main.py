from database import init_db
from agent import process_query

def main():
    print("Initializing AIKart Database")
    init_db()
    print("Database ready.")
    print("Type 'exit' or 'quit' to end session.\n")

    while True:
        try:
            user_input = input("Customer: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ["exit", "quit"]:
                print("\nEnding customer session. Goodbye!")
                break

            agent_response = process_query(user_input)
            print(f"\nAIKart Agent: {agent_response}\n" + "-"*50)

        except KeyboardInterrupt:
            print("\nSession interrupted. Exiting.")
            break
        except Exception as e:
            print(f"\n[Error]: {e}\n")

if __name__ == "__main__":
    main()