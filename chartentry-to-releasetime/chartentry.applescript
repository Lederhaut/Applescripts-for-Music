-- chartentry.applescript
-- Frontend for the macOS Music app.
-- Loops over the user's currently selected tracks, asks chartentry.py
-- to find the chart-entry date on offiziellecharts.de, and refreshes
-- the track in Music.app afterwards.
--
-- Place this script in:
--   ~/Library/Music/Scripts/
-- and enable the Script Menu in Music.app.

-- ---------------------------------------------------------------------------
-- Configuration
-- ---------------------------------------------------------------------------

-- Folder containing chartentry.py and the python venv "pyenv"
-- Determined at runtime as the directory of this script.
property scriptFolder : ""

on getScriptFolder()
	-- Return the POSIX path of the directory containing this script.
	set myPath to POSIX path of (path to me)
	-- Strip trailing slash if present
	if myPath ends with "/" then
		set myPath to text 1 thru -2 of myPath
	end if
	set AppleScript's text item delimiters to "/"
	set pathItems to text items of myPath
	set AppleScript's text item delimiters to "/"
	set parentPath to (items 1 thru -2 of pathItems) as text
	set AppleScript's text item delimiters to ""
	return parentPath
end getScriptFolder

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

on quoteForShell(theText)
	-- Quote a string safely for the shell (single-quoted, escape embedded ').
	set AppleScript's text item delimiters to "'"
	set parts to text items of theText
	set AppleScript's text item delimiters to "'\\''"
	set escaped to parts as text
	set AppleScript's text item delimiters to ""
	return "'" & escaped & "'"
end quoteForShell

on ensureVenv(folder)
	-- Make sure pyenv exists; create + install dependencies if needed.
	set venvPath to folder & "/pyenv"
	set checkCmd to "test -d " & quoteForShell(venvPath) & " && echo OK || echo MISSING"
	set venvState to do shell script checkCmd
	if venvState is "MISSING" then
		log "[INFO] Creating Python virtual environment in " & venvPath
		do shell script "cd " & quoteForShell(folder) & " && python3 -m venv pyenv"
		do shell script "cd " & quoteForShell(folder) & " && source pyenv/bin/activate && pip3 install --quiet --upgrade pip && pip3 install --quiet requests beautifulsoup4 mutagen"
	end if
end ensureVenv

on runChartentry(folder, theArtist, theTitle, thePath)
	set pyCmd to "cd " & quoteForShell(folder) & " && source pyenv/bin/activate && python3 chartentry.py --artist " & quoteForShell(theArtist) & " --title " & quoteForShell(theTitle) & " --file " & quoteForShell(thePath) & " 2>&1"
	try
		set output to do shell script pyCmd
		return output
	on error errMsg number errNum
		return "[ERROR] Shell error " & errNum & ": " & errMsg
	end try
end runChartentry

on extractValueForKey(output, theKey)
	-- Find a line "<theKey>=..." in output. Returns the value or "" if not found.
	set foundValue to ""
	set AppleScript's text item delimiters to {return, linefeed}
	set theLines to text items of output
	set AppleScript's text item delimiters to ""
	set keyPrefix to theKey & "="
	set prefLen to (count of keyPrefix)
	repeat with ln in theLines
		set lnText to ln as text
		if lnText starts with keyPrefix then
			set foundValue to text (prefLen + 1) thru -1 of lnText
			exit repeat
		end if
	end repeat
	return foundValue
end extractValueForKey

on extractScoreLine(output)
	-- Look for a line "SCORE=<n>" in the output and return
	-- "score=<n>" (or "score=0" if not found / value <= 0).
	set scoreValue to my extractValueForKey(output, "SCORE")
	if scoreValue is "" then
		return "score=0"
	end if
	-- Treat negative or non-numeric as 0
	try
		if (scoreValue as integer) > 0 then
			return "score=" & scoreValue
		else
			return "score=0"
		end if
	on error
		return "score=0"
	end try
end extractScoreLine

on prependScoreToComment(theTrack, scoreLine, extraDate)
	-- Prepend scoreLine (and optionally a date line) to track's comment,
	-- separated by linefeeds.
	tell application "Music"
		set existingComment to ""
		try
			set existingComment to (comment of theTrack) as text
		end try
		if extraDate is not "" then
			set newPrefix to scoreLine & linefeed & extraDate
		else
			set newPrefix to scoreLine
		end if
		if existingComment is "" then
			set comment of theTrack to newPrefix
		else
			set comment of theTrack to newPrefix & linefeed & existingComment
		end if
	end tell
end prependScoreToComment

on ensurePlaylistFolder(folderName)
	-- Make sure a folder playlist with the given name exists, return a reference to it.
	tell application "Music"
		if (exists folder playlist folderName) then
			return folder playlist folderName
		else
			set newFolder to make new folder playlist with properties {name:folderName}
			return newFolder
		end if
	end tell
end ensurePlaylistFolder

on ensurePlaylistInFolder(playlistName, parentFolder)
	-- Make sure a user playlist with the given name exists inside parentFolder.
	-- Returns a reference to the playlist.
	tell application "Music"
		set parentName to name of parentFolder
		-- Look for an existing user playlist whose parent matches our folder
		set existingPlaylists to every user playlist whose name is playlistName
		repeat with p in existingPlaylists
			try
				if (name of (parent of p)) is parentName then
					return contents of p
				end if
			end try
		end repeat
		-- Not found: create it inside the folder
		set newPlaylist to make new user playlist with properties {name:playlistName} at parentFolder
		return newPlaylist
	end tell
end ensurePlaylistInFolder

on addTrackToPlaylist(theTrack, thePlaylist)
	tell application "Music"
		try
			duplicate theTrack to thePlaylist
		on error errMsg number errNum
			log "[WARNING] Could not add track to playlist: " & errNum & " " & errMsg
		end try
	end tell
end addTrackToPlaylist

-- ---------------------------------------------------------------------------
-- Main
-- ---------------------------------------------------------------------------

on run
	set scriptFolder to my getScriptFolder()
	log "[INFO] Using script folder: " & scriptFolder
	my ensureVenv(scriptFolder)
	
	-- Ensure playlist folder + playlists exist
	set checkFolder to my ensurePlaylistFolder("CheckReleaseDate")
	set writtenPlaylist to my ensurePlaylistInFolder("Chartentry written", checkFolder)
	set notFoundPlaylist to my ensurePlaylistInFolder("Chartentry date not found", checkFolder)
	set lowScorePlaylist to my ensurePlaylistInFolder("Chartentry low score", checkFolder)
	
	tell application "Music"
		set selectedTracks to selection
		if selectedTracks is {} then
			display dialog "No tracks selected in Music App." buttons {"OK"} default button 1
			return
		end if
		
		set total to count of selectedTracks
		set updatedCount to 0
		set skippedCount to 0
		set failedCount to 0
		
		log "[INFO] Processing " & total & " selected track(s)."
		
		repeat with i from 1 to total
			set t to item i of selectedTracks
			set theArtist to ""
			set theTitle to ""
			set thePath to ""
			
			try
				set theArtist to (artist of t) as text
			end try
			try
				set theTitle to (name of t) as text
			end try
			try
				set theLoc to (location of t)
				set thePath to POSIX path of theLoc
			end try
			
			log "---"
			log "[" & i & "/" & total & "] " & theArtist & " - " & theTitle
			log "    file: " & thePath
			
			if theArtist is "" or theTitle is "" then
				log "[WARNING] Missing artist/title, skipping."
				set skippedCount to skippedCount + 1
				my addTrackToPlaylist(t, notFoundPlaylist)
			else if thePath is "" then
				log "[WARNING] No file location, skipping."
				set skippedCount to skippedCount + 1
				my addTrackToPlaylist(t, notFoundPlaylist)
			else
				set output to my runChartentry(scriptFolder, theArtist, theTitle, thePath)
				log output
				
				-- write best score (or score=0) to comment field, prepended with LF
				set scoreLine to my extractScoreLine(output)
				
				-- if RELEASETIME already existed, the python script wrote
				-- the date to OFFIZIELLECHARTS_DE_ENTRY and signals ALT_TAG=1.
				-- In that case, also include the date in the comment after score.
				set extraDate to ""
				set altFlag to my extractValueForKey(output, "ALT_TAG")
				if altFlag is "1" then
					set theDate to my extractValueForKey(output, "DATE")
					if theDate is not "" then
						set extraDate to theDate
					end if
				end if
				
				my prependScoreToComment(t, scoreLine, extraDate)
				
				-- numeric score for branching (0 if missing/non-numeric)
				set scoreNum to 0
				try
					set scoreRaw to my extractValueForKey(output, "SCORE")
					if scoreRaw is not "" then
						set scoreNum to (scoreRaw as integer)
					end if
				on error
					set scoreNum to 0
				end try
				
				if output contains "DATE=" then
					set updatedCount to updatedCount + 1
					-- refresh track metadata in Music.app
					try
						refresh t
					end try
					my addTrackToPlaylist(t, writtenPlaylist)
					-- if best match score < 100, also add to "low score" playlist
					if scoreNum < 100 then
						my addTrackToPlaylist(t, lowScorePlaylist)
					end if
				else if output contains "[ERROR]" then
					set failedCount to failedCount + 1
				else
					set skippedCount to skippedCount + 1
					my addTrackToPlaylist(t, notFoundPlaylist)
				end if
			end if
		end repeat
		
		-- if there is the letter Œ in the end, replace it with Â
		
		set summary to "Done." & return & Â
			"Updated: " & updatedCount & return & Â
			"Skipped: " & skippedCount & return & Â
			"Failed:  " & failedCount
		log summary
		display dialog summary buttons {"OK"} default button 1
	end tell
end run