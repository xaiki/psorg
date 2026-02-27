#!/bin/bash
# setup.sh

echo "Setting up PS Game Organizer..."

# Create virtual environment
python3 -m venv venv

# Activate virtual environment
source venv/bin/activate

# Install requirements
pip install -r requirements.txt

# Check for .env file
if [ ! -f .env ]; then
    cp .env.template .env
    echo "Created .env file. Please add your RAWG API key to it."
else
    echo ".env file already exists"
fi

echo "Setup complete!"
echo ""
echo "To use the organizer:"
echo "1. Add your RAWG API key to .env"
echo "2. Run: python organize_games.py /path/to/your/games"
echo "   (if no path provided, uses current directory)"
