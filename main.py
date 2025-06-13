import discord
from dotenv import load_dotenv
from discord.ext import commands
from discord import FFmpegPCMAudio
import os
import requests

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
# client = discord.Client(intents=intents)
client = commands.Bot(command_prefix='!', intents=intents)


music_queue = {}



@client.event
async def on_ready():
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('------')

# @client.event
# async def on_message(message):
#     if message.author == client.user:
#         return

#     if message.content.startswith('!hello'):
#         await message.channel.send('Hello!')

# Send welcome and goodbye messages when members join or leave the server
@client.event
async def on_member_join(member):
    channel = client.get_channel(1380955201074106449)
    print(f'{member.name} has joined the server.')
    await channel.send(f'Welcome {member.mention} to the server!')  

@client.event
async def on_member_remove(member):
    channel = client.get_channel(1380955201074106449)
    print(f'{member.name} has left the server.')
    await channel.send(f'Goodbye {member.mention}, we will miss you!')


@client.command()
async def dadjoke(ctx):
    jokeurl = "https://dad-jokes.p.rapidapi.com/random/joke"
    headers = {
        "x-rapidapi-key": "ed02e795f4msh115d528700c32ecp199d9ajsne05ab2038aa0",
        "x-rapidapi-host": "dad-jokes.p.rapidapi.com"
    }
    response = requests.get(jokeurl, headers=headers)
    if response.status_code == 200:
        joke_data = response.json()
        joke = joke_data['body'][0]['setup'] + " " + joke_data['body'][0]['punchline']
        await ctx.send(joke)
    else:
        await ctx.send("Sorry, I couldn't fetch a joke at the moment.")
    print(response.json())


# Command to have bot join author's voice channel
@client.command(pass_context=True)
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.message.author.voice.channel
        await ctx.send(f'Joined {ctx.author.name}\'s voice channel.')
        voice = await channel.connect()
        # source = FFmpegPCMAudio('Yiruma-RiverFlowsInYou.mp3', executable=r"C:\FFmpeg\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe")
        # player = voice.play(source)
    else:
        await ctx.send("You are not connected to a voice channel.")

# Command to have bot leave voice channel
@client.command(pass_context=True)
async def disconnect(ctx):
    if ctx.voice_client:
        await ctx.guild.voice_client.disconnect()
        await ctx.send("Disconnected from voice channel.")
    else:
        await ctx.send("I am not connected to a voice channel.")

def check_queue(ctx, id):
    if id in music_queue and music_queue[id]:
        source = music_queue[id].pop(0)
        ctx.voice_client.play(source, after=lambda e: check_queue(ctx, id))
        return True
    return False


@client.command(pass_context=True)
async def play(ctx, url: str):
    if ctx.voice_client is None:
        if ctx.author.voice:
            channel = ctx.message.author.voice.channel
            await channel.connect()
        else:
            await ctx.send("You are not connected to a voice channel.")
            return

    if ctx.voice_client.is_playing():
        ctx.voice_client.stop()

    if not url.endswith('.mp3'):
        url = url+".mp3"  # Ensure the URL ends with .mp3
    if not os.path.exists(url):
        await ctx.send(f"File {url} does not exist. Please provide a valid URL or ensure the file is downloaded.")
        return
    source = FFmpegPCMAudio(url, executable=r"C:\FFmpeg\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe")
    ctx.voice_client.play(source, after=lambda e=None: check_queue(ctx, ctx.guild.id))
    await ctx.send(f"Now playing: {url}")
        
@client.command(pass_context=True)
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("Paused the music.")
    else:
        await ctx.send("I am not playing any music.")

@client.command(pass_context=True)
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("Resumed the music.")
    else:
        await ctx.send("I am not paused.")

@client.command(pass_context=True)
async def stop(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("Stopped the music.")
    else:
        await ctx.send("I am not playing any music.")




@client.command(pass_context=True)
async def queue(ctx, url: str):
    if not url.endswith('.mp3'):
        url = url+".mp3"  # Ensure the URL ends with .mp3
    if not os.path.exists(url):
        await ctx.send(f"File {url} does not exist. Please provide a valid URL or ensure the file is downloaded.")
        return
    source = FFmpegPCMAudio(url, executable=r"C:\FFmpeg\ffmpeg-master-latest-win64-gpl-shared\bin\ffmpeg.exe")
    guild_id = ctx.guild.id
    if guild_id not in music_queue:
        music_queue[guild_id] = []
    music_queue[guild_id].append(source)
    await ctx.send(f"Added {url} to the queue. Current queue length: {len(music_queue[guild_id])}")


@client.command(pass_context=True)
async def next(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queue and music_queue[guild_id]:
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        if check_queue(ctx, guild_id):
            await ctx.send("Playing next song in the queue.")
        else:
            await ctx.send("No more songs in the queue.")

if __name__ == "__main__":
    print("Starting the bot...")
    load_dotenv()
    TOKEN = os.getenv('DISCORD_TOKEN')
    client.run(TOKEN)